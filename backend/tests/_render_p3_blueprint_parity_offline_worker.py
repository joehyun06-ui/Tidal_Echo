"""Post-merge Render P3 Blueprint parity rehearsal; no external network.

The merged release branch has a safe P3 supervisor but a historically stale
Blueprint startCommand. This worker checks only proposed repo configuration
and OFF-by-default supervisor contracts. It NEVER checks live Render config,
approves deployment, activates browser identity, or writes memory.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.deployment_config import DeploymentConfigError
from backend.memory_oidc_render_drift_review import (
    assess_render_deployment_drift,
)
from backend.tests._support import NoNetworkMixin
from scripts import render_start, render_start_p3

ROOT = Path(__file__).resolve().parents[2]
P3_ENTRY = "python scripts/render_start_p3.py"
BASE_ENTRY = "python scripts/render_start.py"
REVIEW_FLAG = "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED"
BRANCH = "feat/render-telegram-deployment"
SHA = "33f2fb9e09d42a901728355720b8334054c7fab5"


def blueprint():
    return json.loads((ROOT / "render.yaml").read_text(encoding="utf-8"))


def service():
    data = blueprint()
    assert len(data["services"]) == 1
    return data["services"][0]


CONFIG = SimpleNamespace(
    deployment=SimpleNamespace(loop_port=3020),
    relay_port=10000, autonomous_wake_enabled=False,
)
SAFE = {
    "CODEX_CANARY_ENTRYPOINTS_ENABLED": "false",
    "CODEX_GENERATION_ENABLED": "false",
    "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED": "false",
    "MEMORY_OIDC_PINNED_ORIGIN_REHEARSAL_ENABLED": "false",
    "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
}


class P3BlueprintParityTests(NoNetworkMixin, unittest.TestCase):
    def test_blueprint_exact_merged_service_target(self):
        svc = service()
        self.assertEqual(svc["name"], "ouou-home-telegram")
        self.assertEqual(svc["type"], "web")
        self.assertEqual(svc["branch"], BRANCH)
        self.assertEqual(svc["startCommand"], P3_ENTRY)

    def test_explicit_auto_trigger_off_takes_precedence(self):
        svc = service()
        self.assertEqual(svc["autoDeployTrigger"], "off")
        self.assertIs(svc["autoDeploy"], False)

    def test_blueprint_business_preservation(self):
        svc = service()
        self.assertEqual(svc["healthCheckPath"], "/healthz")
        self.assertEqual(svc["plan"], "starter")
        self.assertEqual(svc["numInstances"], 1)
        self.assertEqual(svc["disk"]["mountPath"], "/var/data")
        self.assertEqual(svc["buildCommand"],
                         "python -m pip install -r backend/requirements.txt")

    def test_normal_p3_uses_guard_and_existing_relay(self):
        cmd = render_start_p3.child_commands(
            CONFIG, executable="python-offline", environ=SAFE,
        )
        self.assertEqual(cmd["api_loop"][3],
                         "examples.api_loop_provider_guard:app")
        self.assertEqual(cmd["relay"][3], "backend.p3_relay_app:app")

    def test_codex_p3_is_explicit_only(self):
        env = {**SAFE, "CODEX_GENERATION_ENABLED": "true",
               "CODEX_CANARY_ENTRYPOINTS_ENABLED": "true"}
        cmd = render_start_p3.child_commands(
            CONFIG, executable="python-offline", environ=env,
        )
        self.assertEqual(cmd["api_loop"][3],
                         "examples.api_loop_codex_canary:app")
        self.assertEqual(cmd["relay"][3],
                         "backend.p3_codex_relay_app:app")

    def test_both_services_retain_no_proxy_headers(self):
        for env in (SAFE, {
            **SAFE, "CODEX_GENERATION_ENABLED": "true",
            "CODEX_CANARY_ENTRYPOINTS_ENABLED": "true",
        }):
            with self.subTest(codex=env["CODEX_GENERATION_ENABLED"]):
                cmd = render_start_p3.child_commands(
                    CONFIG, executable="python-offline", environ=env,
                )
                for service_name, host in (("api_loop", "127.0.0.1"),
                                           ("relay", "0.0.0.0")):
                    args = cmd[service_name]
                    self.assertEqual(args.count("--no-proxy-headers"), 1)
                    self.assertNotIn("--proxy-headers", args)
                    self.assertNotIn("--forwarded-allow-ips", args)
                    self.assertEqual(args[args.index("--host") + 1], host)
                    self.assertEqual(args[args.index("--workers") + 1], "1")

    def test_offline_identity_flags_cannot_start_real_p3(self):
        for flag in (
            "MEMORY_OIDC_TRUSTED_PROXY_REVIEW_ENABLED",
            "MEMORY_OIDC_PINNED_ORIGIN_REHEARSAL_ENABLED",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED",
            "MEMORY_OIDC_BROWSER_CALLBACK_REVIEW_ENABLED",
            "MEMORY_PER_USER_AUTH_REVIEW_ENABLED",
            "MEMORY_BROWSER_CONSENT_REVIEW_ENABLED",
        ):
            with self.subTest(flag=flag), self.assertRaisesRegex(
                DeploymentConfigError, "unreviewed_memory_browser_identity_ingress"
            ):
                render_start_p3.child_commands(
                    CONFIG, environ={**SAFE, flag: "true"},
                )

    def test_codex_and_oidc_have_independent_gates(self):
        with self.assertRaisesRegex(
            DeploymentConfigError, "unreviewed_memory_browser_identity_ingress"
        ):
            render_start_p3.child_commands(CONFIG, environ={
                **SAFE,
                "CODEX_GENERATION_ENABLED": "true",
                "CODEX_CANARY_ENTRYPOINTS_ENABLED": "true",
                "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "true",
            })

    def test_oauth_review_envvars_not_enabled_by_blueprint(self):
        env = {row["key"]: row.get("value", None)
               for row in service()["envVars"]}
        for key, value in env.items():
            if key.startswith("MEMORY_OIDC_") and key.endswith(
                ("_ENABLED", "_APPROVED")
            ):
                self.assertEqual(value, "false")
        self.assertNotIn("MEMORY_PER_USER_AUTH_REVIEW_ENABLED", env)
        self.assertNotIn("MEMORY_BROWSER_CONSENT_REVIEW_ENABLED", env)

    def test_paid_api_keys_not_promoted_into_blueprint_values(self):
        env = {row["key"]: row for row in service()["envVars"]}
        self.assertEqual(env["LLM_API_KEY"], {"key": "LLM_API_KEY", "sync": False})
        self.assertEqual(env["KELIVO_API_KEY"], {"key": "KELIVO_API_KEY", "sync": False})
        self.assertEqual(env["RELAY_SECRET"], {"key": "RELAY_SECRET", "sync": False})

    def test_drift_review_understands_revised_p3_blueprint(self):
        with mock.patch.dict(os.environ, {REVIEW_FLAG: "true"}):
            result = assess_render_deployment_drift(
                blueprint(), expected_review_branch=BRANCH,
                expected_review_sha=SHA,
            )
        self.assertTrue(result.blueprint_branch_matches_review)
        self.assertTrue(result.blueprint_auto_deploy_off)
        self.assertTrue(result.blueprint_start_command_expected)
        self.assertFalse(result.may_deploy)
        self.assertFalse(result.may_enable_browser_login)
        self.assertFalse(result.may_write_memory)
        self.assertIn("live_service_snapshot_missing", result.blockers)

    def test_legacy_base_supervisor_entry_is_detected_as_drift(self):
        altered = blueprint()
        altered["services"][0]["startCommand"] = BASE_ENTRY
        with mock.patch.dict(os.environ, {REVIEW_FLAG: "true"}):
            result = assess_render_deployment_drift(
                altered, expected_review_branch=BRANCH,
                expected_review_sha=SHA,
            )
        self.assertFalse(result.blueprint_start_command_expected)
        self.assertIn("blueprint_start_command_unreviewed", result.blockers)

    def test_auto_deploy_enable_triggers_release_block(self):
        altered = blueprint()
        altered["services"][0]["autoDeployTrigger"] = "commit"
        with mock.patch.dict(os.environ, {REVIEW_FLAG: "true"}):
            result = assess_render_deployment_drift(
                altered, expected_review_branch=BRANCH,
                expected_review_sha=SHA,
            )
        self.assertFalse(result.blueprint_auto_deploy_off)
        self.assertIn("blueprint_autodeploy_not_off", result.blockers)

    def test_off_flags_in_external_env_do_not_authorize_p3(self):
        render_start.reject_unreviewed_browser_identity_ingress(SAFE)
        self.assertNotIn("MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED=true",
                         json.dumps(blueprint()))

    def test_no_release_approval_capability_added(self):
        self.assertFalse(hasattr(render_start_p3, "may_deploy"))
        self.assertFalse(hasattr(render_start_p3, "issue_browser_session"))
        self.assertFalse(hasattr(render_start_p3, "authorize_memory_write"))


if __name__ == "__main__":
    unittest.main()
