"""Offline Render ingress release interlock, not a real proxy/IdP assertion.

Verifies production supervisor's preflight and child launch fail closed if
OIDC or browser identity review flags are accidentally enabled. No sockets,
no deployment, no paid API, and no memory writes.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.deployment_config import DeploymentConfigError
from backend.tests._support import NoNetworkMixin
from backend.tests.test_render_deployment import render_env
from scripts import render_start

_ROOT = Path(__file__).resolve().parents[2]
_CATEGORY = "unreviewed_memory_browser_identity_ingress"
_FLAGS = (
    "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED",
    "MEMORY_OIDC_ISOLATED_ASGI_REVIEW_ENABLED",
    "MEMORY_OIDC_CODE_PKCE_REVIEW_ENABLED",
    "MEMORY_OIDC_ID_TOKEN_VERIFIER_REVIEW_ENABLED",
    "MEMORY_OIDC_HTTPS_SENDER_REVIEW_ENABLED",
    "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED",
    "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED",
    "MEMORY_PER_USER_AUTH_REVIEW_ENABLED",
    "MEMORY_BROWSER_CONSENT_REVIEW_ENABLED",
)


def _dummy_config():
    return SimpleNamespace(
        deployment=SimpleNamespace(loop_port=3020), relay_port=3011,
        autonomous_wake_enabled=False, instance_nonce="OFFLINE_NONCE",
        internal_token="OFFLINE_TEST_TOKEN_ONLY", shutdown_grace=0.01,
    )


class RenderIngressInterlockTests(NoNetworkMixin, unittest.TestCase):
    def assert_blocked(self, env):
        with self.assertRaisesRegex(DeploymentConfigError, _CATEGORY):
            render_start.reject_unreviewed_browser_identity_ingress(env)

    def test_missing_activation_flags_remain_off(self):
        render_start.reject_unreviewed_browser_identity_ingress({})
        render_start.reject_unreviewed_browser_identity_ingress({
            "MEMORY_OIDC_ISSUER": "https://login.example.test",
            "OPENAI_API_KEY": "",
        })

    def test_every_known_review_activation_is_blocked(self):
        for name in _FLAGS:
            with self.subTest(flag=name):
                self.assert_blocked({name: "true"})

    def test_explicit_strict_false_accepted(self):
        render_start.reject_unreviewed_browser_identity_ingress({
            name: "false" for name in _FLAGS
        })

    def test_ambiguous_values_not_accepted_as_disabled(self):
        for value in ("TRUE", "True", "1", "on", "false ", " false",
                      "FALSE", "0", "False", "", "yes", None, True):
            with self.subTest(value=repr(value)):
                self.assert_blocked({"MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": value})

    def test_future_oidc_enabled_or_approved_flags_fail_closed(self):
        for name in ("MEMORY_OIDC_FUTURE_LOGIN_ENABLED",
                     "MEMORY_OIDC_FUTURE_REMOTE_APPROVED"):
            self.assert_blocked({name: "true"})
            render_start.reject_unreviewed_browser_identity_ingress({name: "false"})

    def test_flags_cannot_bypass_by_mixed_case_name(self):
        for name in ("memory_oidc_real_network_explicitly_approved",
                     "Memory_Oidc_Trusted_Proxy_Review_Enabled",
                     "memory_per_user_auth_review_enabled"):
            with self.subTest(name=name):
                self.assert_blocked({name: "true"})

    def test_unrelated_configuration_may_remain_configured(self):
        render_start.reject_unreviewed_browser_identity_ingress({
            "MEMORY_OIDC_ISSUER": "https://login.example.test",
            "MEMORY_OIDC_CLIENT_ID": "offline-client",
            "RELAY_SECRET": "dummy",
        })

    def test_invalid_mapping_is_denied_without_leaking_value(self):
        for env in ({None: "true"}, {"MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": object()}):
            with self.subTest(case=len(env)):
                self.assert_blocked(env)

    def test_render_preflight_refuses_before_parser_or_path_creation(self):
        with mock.patch.object(render_start.TelegramConfig, "from_env") as parsed, \
             mock.patch.object(render_start, "load_deployment_config") as loaded, \
             mock.patch.object(render_start.deployment_config,
                               "prepare_persistent_paths") as paths:
            with self.assertRaisesRegex(DeploymentConfigError, _CATEGORY):
                render_start.preflight({
                    "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "true"
                })
            parsed.assert_not_called()
            loaded.assert_not_called()
            paths.assert_not_called()

    def test_render_preflight_allows_actual_synthetic_telegram_config_off(self):
        with tempfile.TemporaryDirectory() as directory:
            env = render_env(Path(directory))
            env.update({"MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false",
                        "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false"})
            config = render_start.preflight(env)
            self.assertEqual(config.relay_port, 10000)
            commands = render_start.child_commands(config, "python-review")
            self.assertIn("--no-proxy-headers", commands["relay"])
            self.assertIn("--no-proxy-headers", commands["api_loop"])

    def test_child_environment_denies_flags_even_if_preflight_bypassed(self):
        for name in _FLAGS:
            with self.subTest(name=name):
                with self.assertRaisesRegex(DeploymentConfigError, _CATEGORY):
                    render_start.child_environment(
                        _dummy_config(), {name: "true"}
                    )

    def test_child_environment_keeps_off_values_and_server_only_token_flow(self):
        env = render_start.child_environment(
            _dummy_config(), {
                "OPENAI_API_KEY": "",
                "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false",
                "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
                "API_LOOP_INTERNAL_TOKEN": "do-not-inherit",
                "API_LOOP_INSTANCE_NONCE": "do-not-inherit",
            }
        )
        self.assertEqual(env["MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED"], "false")
        self.assertEqual(env["RELAY_URL"], "http://127.0.0.1:3011")
        self.assertNotIn("API_LOOP_INTERNAL_TOKEN", env)
        self.assertNotIn("API_LOOP_INSTANCE_NONCE", env)

    def test_supervisor_refuses_to_spawn_children_when_flag_enabled(self):
        with mock.patch.object(render_start.signal, "signal", return_value=None):
            invoked = mock.Mock()
            runner = render_start.RenderSupervisor(
                popen=invoked, health_probe=lambda *a: True,
                logger=lambda *args: None,
                sleep=lambda _seconds: None,
            )
            result = runner.run(_dummy_config(), {
                "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "true"
            })
            self.assertEqual(result, 1)
            invoked.assert_not_called()
            self.assertEqual(runner.processes, {})

    def test_repository_render_blueprint_does_not_activate_oidc_review(self):
        blueprint = json.loads((_ROOT / "render.yaml").read_text(encoding="utf-8"))
        services = [service for service in blueprint["services"]
                    if service.get("name") == "ouou-home-telegram"]
        self.assertEqual(len(services), 1)
        service = services[0]
        self.assertIs(service["autoDeploy"], False)
        self.assertEqual(service["autoDeployTrigger"], "off")
        self.assertEqual(service["startCommand"], "python scripts/render_start_p3.py")
        self.assertEqual(service["type"], "web")
        self.assertEqual(service["numInstances"], 1)
        env = {row["key"]: row.get("value", "unset") for row in service["envVars"]}
        self.assertFalse(any(
            name.startswith("MEMORY_OIDC_") and
            name.endswith(("_ENABLED", "_APPROVED")) and value != "false"
            for name, value in env.items()
        ))
        self.assertNotIn("MEMORY_PER_USER_AUTH_REVIEW_ENABLED", env)

    def test_blueprint_is_not_a_production_trusted_peer_attestation(self):
        blueprint = json.loads((_ROOT / "render.yaml").read_text(encoding="utf-8"))
        service = next(service for service in blueprint["services"]
                       if service.get("name") == "ouou-home-telegram")
        # A public web service is NOT proved to have an authenticated raw
        # socket peer and proxy sanitization rule by blueprint metadata.
        self.assertEqual(service["type"], "web")
        self.assertNotIn("raw_socket_peer_provider", service)
        self.assertNotIn("ingress_header_sanitization_proof", service)
        commands = render_start.child_commands(_dummy_config(), "python-review")
        self.assertEqual(commands["relay"][5], "0.0.0.0")

    def test_no_runtime_grant_or_production_authentication_is_added(self):
        self.assertFalse(hasattr(render_start, "issue_browser_session"))
        self.assertFalse(hasattr(render_start, "remember"))
        self.assertFalse(hasattr(render_start, "authorize_memory_write"))


if __name__ == "__main__":
    unittest.main()
