"""Stage 36: safe, offline-only tests of optional operator-local HTTPS evidence GET."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from datetime import datetime, timezone

from scripts import render_operator_evidence_readonly as obs

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "12345678:" + "A" * 40
KEY = "rnd_" + "B" * 40
NOW = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)

class Response:
    def __init__(self, data, *, status=200, content_type="application/json"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.body = data if type(data) is bytes else json.dumps(data).encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self, n):
        if n != obs.MAX_RESPONSE_BYTES + 1:
            raise AssertionError("unbounded response")
        return self.body[:n]

class OperatorReadonlyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response = Response({"ok": True, "result": {
            "url": obs.EXPECTED_WEBHOOK, "pending_update_count": 0
        }})
    def fake(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response
    def run_source(self, source="telegram", *, approved=True, env=None, token=None):
        secret = token if token is not None else TOKEN if source == "telegram" else KEY
        return obs.collect(source, approved=approved, environ={obs.APPROVAL_FLAG: "true"} if env is None else env,
                           secret_reader=lambda prompt: secret, opener=self.fake, now=NOW)

    def test_default_off_blocks_request_and_secret_prompt(self):
        calls = []
        r = obs.collect("telegram", environ={}, approved=False,
                        secret_reader=lambda x: calls.append(x), opener=self.fake)
        self.assertEqual(r["status"], "disabled")
        self.assertFalse(calls)
        self.assertFalse(self.requests)

    def test_both_explicit_gates_mandatory(self):
        for approved, env in ((True, {}), (False, {obs.APPROVAL_FLAG: "true"}),
                              (True, {obs.APPROVAL_FLAG: "TRUE"}),
                              (True, {obs.APPROVAL_FLAG: "true "}), (True, {obs.APPROVAL_FLAG: "1"})):
            with self.subTest(approved=approved, env="redacted"):
                self.assertEqual(self.run_source(approved=approved, env=env)["status"], "disabled")
        self.assertFalse(self.requests)

    def test_blocks_other_sources_without_prompt(self):
        for name in ("healthz", "readyz", "setWebhook", "restore", "https://example.test"):
            with self.subTest():
                self.assertEqual(self.run_source(name)["status"], "blocked")
        self.assertFalse(self.requests)

    def test_fixed_telegram_get_only_and_no_auth(self):
        r = self.run_source()
        self.assertEqual(r["status"], "registered_claim_only")
        req, timeout = self.requests[0]
        self.assertEqual(req.full_url, obs.TELEGRAM_ORIGIN + "/bot" + TOKEN + "/getWebhookInfo")
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(timeout, 5)
        self.assertIsNone(req.get_header("Authorization"))
        self.assertIsNone(req.get_header("Cookie"))

    def test_fixed_render_get_only_and_authorization(self):
        self.response = Response([{"createdAt": "2026-10-28T12:00:00Z",
                                   "snapshotKey": "SENSITIVE_KEY", "instanceId": "test"}], status=201)
        r = self.run_source("snapshots")
        self.assertEqual(r["status"], "recent_metadata_restore_unverified")
        req, timeout = self.requests[0]
        self.assertEqual(req.full_url, obs.RENDER_ORIGIN + "/v1/disks/" + obs.RENDER_DISK_ID + "/snapshots")
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(req.get_header("Authorization"), "Bearer " + KEY)
        self.assertIsNone(req.get_header("Cookie"))
        self.assertEqual(timeout, 5)

    def test_tls_no_proxy_redirect_in_live_transport_constructor(self):
        class FakeOpener:
            def open(self, request, timeout):
                return Response({"ok": True, "result": {"url": obs.EXPECTED_WEBHOOK, "pending_update_count": 0}})
        with mock.patch("urllib.request.build_opener", return_value=FakeOpener()) as build, mock.patch(
            "ssl.create_default_context", return_value=object()
        ) as tls:
            r = obs.collect("telegram", approved=True, environ={obs.APPROVAL_FLAG: "true"},
                            secret_reader=lambda _: TOKEN)
        self.assertEqual(r["status"], "registered_claim_only")
        tls.assert_called_once_with()
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[2], obs._NoRedirect)
        self.assertIsNone(handlers[2].redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_telegram_missing_registration(self):
        self.response = Response({"ok": True, "result": {"url": ""}})
        self.assertEqual(self.run_source()["status"], "not_registered")

    def test_telegram_exact_target_including_query(self):
        for target in (obs.EXPECTED_WEBHOOK + "?secret=X", obs.EXPECTED_WEBHOOK + "/", "https://evil.test/x"):
            self.response = Response({"ok": True, "result": {"url": target, "pending_update_count": 0}})
            self.assertEqual(self.run_source()["status"], "target_mismatch")

    def test_telegram_history_is_not_current_failure(self):
        self.response = Response({"ok": True, "result": {
            "url": obs.EXPECTED_WEBHOOK, "pending_update_count": 3,
            "last_error_date": 123, "last_error_message": "BOT_TOKEN_PRIVATE",
            "last_synchronization_error_date": 987, "allowed_updates": ["message"]
        }})
        r = self.run_source()
        self.assertTrue(r["historical_delivery_error"])
        self.assertTrue(r["historical_sync_error"])
        self.assertEqual(r["pending_updates"], 3)
        self.assertTrue(r["message_update_allowed"])
        self.assertNotIn("BOT_TOKEN_PRIVATE", json.dumps(r))
        self.assertNotIn(obs.EXPECTED_WEBHOOK, json.dumps(r))

    def test_telegram_invalid_types(self):
        for d in ({"url": 99}, {"url": obs.EXPECTED_WEBHOOK, "pending_update_count": "0"},
                  {"url": obs.EXPECTED_WEBHOOK, "pending_update_count": True},
                  {"url": obs.EXPECTED_WEBHOOK, "pending_update_count": -1},
                  {"url": obs.EXPECTED_WEBHOOK, "pending_update_count": 0, "last_error_date": "1"},
                  {"url": obs.EXPECTED_WEBHOOK, "pending_update_count": 0, "allowed_updates": [5]}):
            self.response = Response({"ok": True, "result": d})
            self.assertEqual(self.run_source()["status"], "invalid_response")

    def test_invalid_or_missing_credentials_no_request(self):
        for source, key in (("telegram", ""), ("telegram", "not-a-token"),
                            ("snapshots", "key with spaces"), ("snapshots", "x")):
            with self.subTest(source=source):
                self.assertEqual(self.run_source(source, token=key)["status"], "invalid_local_credential")
        self.assertFalse(self.requests)

    def test_snapshot_empty_list_not_proven_backup(self):
        self.response = Response([])
        r = self.run_source("snapshots")
        self.assertEqual(r["status"], "no_snapshot_records")
        self.assertEqual(r["snapshot_count"], 0)

    def test_snapshot_latest_utc_and_redacts_key(self):
        self.response = Response([{"createdAt": "2026-10-27T11:00:00Z", "snapshotKey": "PRIVATE_KEY_A"},
                                  {"createdAt": "2026-10-28T20:00:00+08:00", "snapshotKey": "PRIVATE_KEY_B"}])
        r = self.run_source("snapshots")
        self.assertEqual(r["snapshot_count"], 2)
        self.assertEqual(r["latest_created_at_utc"], "2026-10-28T12:00:00Z")
        self.assertNotIn("PRIVATE_KEY", json.dumps(r))

    def test_snapshot_stale_not_restorable_proof(self):
        self.response = Response([{"createdAt": "2026-10-20T01:00:00Z", "snapshotKey": "PRIVATE"}])
        self.assertEqual(self.run_source("snapshots")["status"], "old_metadata_restore_unverified")

    def test_snapshot_future_timestamp_invalid(self):
        self.response = Response([{"createdAt": "2026-10-29T05:00:00Z", "snapshotKey": "PRIVATE"}])
        self.assertEqual(self.run_source("snapshots")["status"], "clock_or_timestamp_inconsistent")

    def test_snapshot_invalid_item_blocked(self):
        for content in ([{"createdAt": "no", "snapshotKey": "X"}],
                        [{"createdAt": "2026-10-28T12:00:00Z"}], [{"snapshotKey": "K"}],
                        {"not": "list"}, [None]):
            self.response = Response(content)
            self.assertEqual(self.run_source("snapshots")["status"], "invalid_response")

    def test_http_non_success_and_redirect_rejected(self):
        for status in (301, 302, 403, 502, 503):
            self.response = Response({"ok": True}, status=status)
            r = self.run_source()
            self.assertNotEqual(r["status"], "registered_claim_only")
            self.assertEqual(r["http_status"], status)

    def test_http_error_exception_does_not_leak_token_url(self):
        def raise_error(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 502, "SENSITIVE_MESSAGE", {}, None)
        r = obs.collect("telegram", approved=True, environ={obs.APPROVAL_FLAG: "true"},
                        secret_reader=lambda _: TOKEN, opener=raise_error)
        self.assertEqual(r["status"], "http_error")
        self.assertEqual(r["http_status"], 502)
        self.assertNotIn(TOKEN, json.dumps(r))
        self.assertNotIn("SENSITIVE_MESSAGE", json.dumps(r))

    def test_dns_tls_timeout_redacted(self):
        def raise_error(request, timeout):
            raise OSError("TOKEN_PRIVATE " + TOKEN)
        r = obs.collect("telegram", approved=True, environ={obs.APPROVAL_FLAG: "true"},
                        secret_reader=lambda _: TOKEN, opener=raise_error)
        self.assertEqual(r["status"], "unavailable")
        self.assertNotIn(TOKEN, json.dumps(r))

    def test_content_type_and_size_bounds(self):
        for body, content_type in ((b"X" * (obs.MAX_RESPONSE_BYTES + 1), "application/json"),
                                   (b"{}", "text/html"), (b"{", "application/json")):
            self.response = Response(body, content_type=content_type)
            self.assertEqual(self.run_source()["status"], "invalid_response")

    def test_no_network_when_disabled_even_if_sockets_denied(self):
        with mock.patch("socket.socket", side_effect=AssertionError("NETWORK FORBIDDEN")):
            self.assertEqual(self.run_source(approved=False)["status"], "disabled")

    def test_no_release_authority_in_any_result(self):
        self.response = Response([])
        for r in (self.run_source("snapshots"), self.run_source("telegram", approved=False)):
            for k in ("may_deploy", "may_rollback", "may_change_telegram", "may_restore_disk",
                      "may_enable_oidc", "may_write_memory", "may_use_paid_api"):
                self.assertIs(r[k], False)

    def test_cli_default_off_is_nonzero_and_redacted(self):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "render_operator_evidence_readonly.py"),
                               "--source", "telegram"], capture_output=True, text=True,
                              cwd=ROOT, timeout=10,
                              env={**os.environ, obs.APPROVAL_FLAG: "false"})
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["status"], "disabled")
        self.assertFalse(proc.stderr)

if __name__ == "__main__":
    unittest.main()
