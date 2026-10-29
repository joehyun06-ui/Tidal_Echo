"""No-network regression of pinned public Render health/readiness observation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from backend.render_public_health_probe_readonly import (
    FLAG, ORIGIN, PATHS, CHECKS, _RejectRedirect, _from_json,
    probe_render_public_health,
)
from backend.tests._support import NoNetworkMixin

ROOT = Path(__file__).resolve().parents[2]


class _Response:
    status = 200
    def __init__(self, obj, *, status=200, ctype="application/json"):
        self.status = status
        self.raw = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.headers = {"Content-Type": ctype}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        if limit != 4097:
            raise AssertionError("body read must be bounded")
        return self.raw[:limit]


class RenderPublicReadOnlyHealthTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.response = _Response({"ok": True})
        self.env = {FLAG: "true"}
    def fake(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response
    def check(self, kind="healthz", env=None):
        return probe_render_public_health(
            kind, environ=self.env if env is None else env, open_readonly=self.fake,
        )
    def test_default_off_requires_single_explicit_flag(self):
        result = self.check(env={})
        self.assertEqual(result.status, "disabled")
        self.assertEqual(self.requests, [])
        for value in ("TRUE", "1", "yes", "false", "", "true "):
            with self.subTest(value=value):
                self.assertEqual(self.check(env={FLAG: value}).status, "disabled")
    def test_no_arbitrary_url_or_webhook_endpoint(self):
        for endpoint in (
            "", "/healthz", "https://evil.test/healthz",
            "integrations/telegram/webhook", "readyz?key=x", "oauth/callback",
        ):
            with self.subTest(endpoint=endpoint):
                r = self.check(endpoint)
                self.assertEqual(r.status, "blocked")
                self.assertIsNone(r.http_status)
        self.assertEqual(self.requests, [])
    def test_fixed_https_origin_and_get_only_no_auth_header(self):
        result = self.check()
        self.assertEqual(result.status, "http_observed")
        self.assertEqual(result.http_status, 200)
        request, timeout = self.requests[0]
        self.assertEqual(request.full_url, ORIGIN + PATHS["healthz"])
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 5)
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(request.data, None)
    def test_real_transport_config_disables_env_proxy_and_redirects(self):
        class FakeOpener:
            def open(self, req, timeout):
                return _Response({"ok": True})
        with mock.patch("urllib.request.build_opener", return_value=FakeOpener()) as build, mock.patch(
            "ssl.create_default_context", return_value=object()
        ) as context:
            result = probe_render_public_health("healthz", environ=self.env)
        self.assertEqual(result.status, "http_observed")
        context.assert_called_once_with()
        args = build.call_args.args
        self.assertEqual(len(args), 3)
        self.assertEqual(args[0].proxies, {})
        self.assertIsInstance(args[2], _RejectRedirect)
        self.assertIsNone(args[2].redirect_request(None, None, 302, "", {}, "https://evil.test"))
    def test_minimal_health_json_exactly_true(self):
        self.assertEqual(self.check().status, "http_observed")
        for obj in ({"ok": 1}, {"ok": False}, {"ok": "true"},
                    {"ok": True, "env": "secret"}, [], None):
            with self.subTest(obj=obj):
                self.response = _Response(obj)
                self.assertEqual(self.check().status, "invalid_response")
    def test_readyz_status_and_check_names_are_bounded(self):
        self.response = _Response({
            "ready": True, "status": "ready",
            "checks": {"database": True, "telegram_config": True,
                       "telegram_worker": True, "api_loop": True,
                       "persistent_path": True, "memory_core": False},
        })
        result = self.check("readyz")
        self.assertEqual(result.status, "ready_observed")
        self.assertEqual(result.check_count, 6)
        self.assertEqual(result.failed_checks, ("memory_core",))
        self.assertEqual(self.requests[0][0].full_url, ORIGIN + "/readyz")
    def test_readyz_200_with_not_ready_payload_is_inconsistent(self):
        self.response = _Response({
            "ready": False, "status": "not_ready",
            "checks": {"database": False, "api_loop": True},
            "errors": {"database": "core_schema_invalid"},
        })
        result = self.check("readyz")
        self.assertEqual(result.status, "invalid_response")
        self.assertEqual(result.http_status, 200)

    def test_readyz_ready_claim_cannot_mask_failed_database(self):
        self.response = _Response({
            "ready": True, "status": "ready",
            "checks": {"database": False, "api_loop": True},
        })
        result = self.check("readyz")
        self.assertEqual(result.status, "invalid_response")
    def test_malformed_readyz_payload_is_rejected(self):
        variants = (
            {"ready": 1, "status": "ready", "checks": {"database": True}},
            {"ready": True, "status": "not_ready", "checks": {"database": True}},
            {"ready": True, "status": "ready", "checks": {}},
            {"ready": True, "status": "ready", "checks": {"secret": True}},
            {"ready": True, "status": "ready", "checks": {"database": 1}},
            {"ready": True, "status": "ready", "checks": {"database": True}, "token": "fake"},
            {"ready": False, "status": "ready", "checks": {"database": False}},
        )
        for obj in variants:
            with self.subTest(case=obj):
                self.response = _Response(obj)
                self.assertEqual(self.check("readyz").status, "invalid_response")
    def test_bounded_json_and_content_type(self):
        for payload, content_type in (
            (b"x" * 4097, "application/json"),
            (b'{"ok":true}', "text/html"),
            (b'{broken', "application/json"),
            (b'\xff', "application/json"),
        ):
            with self.subTest(size=len(payload),type=content_type):
                self.response = _Response(payload, ctype=content_type)
                self.assertEqual(self.check().status, "invalid_response")
    def test_non_200_cannot_claim_health_even_with_valid_body(self):
        for status in (201, 204, 301, 302, 401, 403, 404, 502, 503):
            with self.subTest(status=status):
                self.response = _Response({"ok": True}, status=status)
                r = self.check()
                self.assertNotEqual(r.status, "http_observed")
                self.assertEqual(r.http_status, status)
    def test_http_errors_do_not_expose_response_body(self):
        def bad(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 502, "SYNTHETIC_SECRET", {}, None)
        r = probe_render_public_health("healthz", environ=self.env, open_readonly=bad)
        self.assertEqual(r.status, "http_error")
        self.assertEqual(r.http_status, 502)
        self.assertNotIn("SYNTHETIC_SECRET", repr(r))
    def test_dns_tls_and_redirect_failures_fail_closed(self):
        for error in (OSError("SYNTHETIC_SECRET"), RuntimeError("untrusted_redirect")):
            with self.subTest(error=type(error).__name__):
                def raises(req, timeout):
                    raise error
                r = probe_render_public_health("healthz", environ=self.env, open_readonly=raises)
                self.assertEqual(r.status, "unavailable")
                self.assertNotIn("SYNTHETIC_SECRET", repr(r))
    def test_success_does_not_grant_rollback_or_identity(self):
        r = self.check()
        for attr in ("may_deploy", "may_rollback", "may_enable_oidc",
                     "may_write_memory", "may_use_paid_api"):
            self.assertIs(getattr(r, attr), False)
    def test_no_telegram_api_or_webhook_action_is_available(self):
        r = self.check()
        for attr in ("telegram_send", "telegram_get_webhook_info", "set_webhook",
                     "delete_webhook", "rollback", "deploy"):
            self.assertFalse(hasattr(r, attr))
    def test_cli_default_is_disabled_and_exit_code_cannot_authorize(self):
        run = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "render_public_health_readonly.py"),
            "--endpoint", "healthz",
        ], capture_output=True, text=True, timeout=15, cwd=ROOT,
           env={**os.environ, FLAG: "false"}, check=False)
        self.assertEqual(run.returncode, 2)
        data = json.loads(run.stdout)
        self.assertEqual(data["status"], "disabled")
        self.assertFalse(data["may_deploy"])
        self.assertFalse(data["may_rollback"])
        self.assertEqual(run.stderr, "")
    def test_public_health_is_not_telegram_or_disk_readiness(self):
        self.assertEqual(self.check().check_count, 0)
        self.assertEqual(self.check().failed_checks, ())
        self.assertIn("database", CHECKS)
        self.assertIn("telegram_worker", CHECKS)
    def test_repo_healthz_remains_minimal_not_readiness(self):
        c = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
        self.assertIn('async def healthz():\n    return {"ok": True}', c)
        self.assertIn('@app.get("/readyz")', c)
        self.assertIn('"telegram_worker": worker_task is not None', c)
    def test_blueprint_only_points_to_liveness_not_readiness(self):
        b = json.loads((ROOT / "render.yaml").read_text(encoding="utf-8"))
        self.assertEqual(b["services"][0]["healthCheckPath"], "/healthz")
        self.assertEqual(b["services"][0]["disk"]["mountPath"], "/var/data")
    def test_webhook_setup_is_not_a_safe_readonly_probe(self):
        source = (ROOT / "scripts" / "configure_telegram_webhook.py").read_text(encoding="utf-8")
        self.assertIn("/setWebhook", source)
        module = (ROOT / "backend" / "render_public_health_probe_readonly.py").read_text(encoding="utf-8")
        self.assertNotIn("setWebhook", module)
        self.assertNotIn("getWebhookInfo", module)


if __name__ == "__main__":
    unittest.main()
