"""Synthetic, no-network Render deployment drift contract checks."""
from __future__ import annotations

import json
import os
import unittest
import io
import tempfile
import sys
import subprocess
from contextlib import redirect_stdout
from scripts import memory_oidc_render_drift_check_offline as checker
from pathlib import Path
from unittest import mock

from backend.memory_oidc_render_drift_review import (
    RenderDriftReviewError,
    assess_render_deployment_drift,
)
from backend.tests._support import NoNetworkMixin

_ROOT = Path(__file__).resolve().parents[2]
_HEAD = "34a8872ca77a1f11d716514a6c1ff9e6ef0a0e2b"
_BRANCH = "test/memory-oidc-render-ingress-release-interlock-offline-20261028"


def blueprint():
    return json.loads((_ROOT / "render.yaml").read_text(encoding="utf-8"))


def snapshot(**changes):
    values = {
        "service_name": "ouou-home-telegram",
        "service_type": "web_service",
        "repository_full_name": "joehyun06-ui/Tidal_Echo",
        "branch": _BRANCH,
        "deployed_commit_sha": _HEAD,
        "start_command": "python scripts/render_start.py",
        "auto_deploy_trigger": "off",
        "deploy_status": "live",
    }
    values.update(changes)
    return values


class RenderDriftReviewTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {
            "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
            "OPENAI_API_KEY": "",
        })
        patch.start()
        self.addCleanup(patch.stop)

    def review(self, *, source=None, snap=None, branch=_BRANCH, sha=_HEAD):
        return assess_render_deployment_drift(
            blueprint() if source is None else source,
            expected_review_branch=branch,
            expected_review_sha=sha,
            snapshot=snap,
        )

    def deny(self, **kwargs):
        with self.assertRaisesRegex(
            RenderDriftReviewError, "memory_oidc_render_drift_review_unavailable"
        ):
            self.review(**kwargs)

    def test_default_off_rejects_even_valid_inputs(self):
        os.environ.pop("MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED")
        self.deny(snap=snapshot())

    def test_actual_blueprint_review_branch_drift_and_no_live_snapshot(self):
        result = self.review()
        self.assertFalse(result.blueprint_branch_matches_review)
        self.assertTrue(result.blueprint_auto_deploy_off)
        self.assertTrue(result.blueprint_start_command_expected)
        self.assertFalse(result.snapshot_supplied)
        self.assertIsNone(result.recorded_commit_matches_review)
        self.assertIn("blueprint_branch_differs_from_review", result.blockers)
        self.assertIn("live_service_snapshot_missing", result.blockers)
        self.assertFalse(result.may_deploy)
        self.assertFalse(result.may_enable_browser_login)
        self.assertFalse(result.may_write_memory)

    def test_manual_perfect_snapshot_still_cannot_authorize(self):
        result = self.review(snap=snapshot())
        self.assertTrue(result.snapshot_supplied)
        self.assertTrue(result.recorded_branch_matches_review)
        self.assertTrue(result.recorded_commit_matches_review)
        self.assertTrue(result.recorded_start_command_matches_blueprint)
        self.assertTrue(result.recorded_auto_deploy_off)
        self.assertIn("snapshot_source_not_authenticated", result.blockers)
        self.assertIn("edge_forwarded_headers_not_attested", result.blockers)
        self.assertIn("raw_socket_peer_not_attested", result.blockers)
        self.assertFalse(result.may_deploy)
        self.assertFalse(result.may_enable_browser_login)
        self.assertFalse(result.may_write_memory)

    def test_report_repr_never_leaks_snapshot_content(self):
        result = self.review(snap=snapshot())
        rendered = repr(result)
        self.assertNotIn("joehyun06", rendered)
        self.assertNotIn(_HEAD, rendered)
        self.assertNotIn(_BRANCH, rendered)

    def test_recorded_branch_and_commit_drift_detected(self):
        result = self.review(snap=snapshot(
            branch="feat/render-telegram-deployment",
            deployed_commit_sha="a" * 40,
        ))
        self.assertFalse(result.recorded_branch_matches_review)
        self.assertFalse(result.recorded_commit_matches_review)
        self.assertIn("recorded_branch_differs_from_review", result.blockers)
        self.assertIn("recorded_commit_differs_from_review", result.blockers)

    def test_recorded_repository_and_command_mismatch_detected(self):
        result = self.review(snap=snapshot(
            repository_full_name="another-org/another-repository",
            start_command="python unwanted.py",
        ))
        self.assertFalse(result.recorded_start_command_matches_blueprint)
        self.assertIn("recorded_repository_mismatch", result.blockers)
        self.assertIn("recorded_start_command_differs_from_blueprint", result.blockers)

    def test_recorded_autodeploy_and_deploy_status_not_trusted(self):
        result = self.review(snap=snapshot(
            auto_deploy_trigger="commit", deploy_status="not_live",
        ))
        self.assertFalse(result.recorded_auto_deploy_off)
        self.assertIn("recorded_autodeploy_not_off", result.blockers)
        self.assertIn("recorded_deployment_not_live", result.blockers)

    def test_source_blueprint_autodeploy_trigger_precedence(self):
        doc = blueprint()
        service = doc["services"][0]
        service["autoDeploy"] = False
        service["autoDeployTrigger"] = "commit"
        result = self.review(source=doc)
        self.assertFalse(result.blueprint_auto_deploy_off)
        self.assertIn("blueprint_autodeploy_not_off", result.blockers)
        service["autoDeployTrigger"] = "off"
        service["autoDeploy"] = True
        self.assertTrue(self.review(source=doc).blueprint_auto_deploy_off)

    def test_source_blueprint_start_command_changed_detected(self):
        doc = blueprint()
        doc["services"][0]["startCommand"] = "uvicorn backend.app:app"
        self.assertFalse(self.review(source=doc).blueprint_start_command_expected)
        self.assertIn("blueprint_start_command_unreviewed",
                      self.review(source=doc).blockers)

    def test_source_blueprint_wrong_type_and_missing_service_denied(self):
        for data in ({}, {"services": []},
                     {"services": [dict(blueprint()["services"][0], type="worker")]}):
            with self.subTest(keys=tuple(data)):
                self.deny(source=data)

    def test_source_blueprint_duplicate_target_service_denied(self):
        doc = blueprint()
        doc["services"].append(dict(doc["services"][0]))
        self.deny(source=doc)

    def test_source_blueprint_bad_command_denied(self):
        doc = blueprint()
        doc["services"][0]["startCommand"] = "python\nunsafe.py"
        self.deny(source=doc)

    def test_source_branch_and_sha_must_be_canonical(self):
        for branch in ("/bad", "a//b", "a..b", "abc ", "", "../main"):
            with self.subTest(branch=branch):
                self.deny(branch=branch)
        for sha in ("abc123", "A" * 40, "f" * 39, "g" * 40):
            with self.subTest(sha=sha):
                self.deny(sha=sha)

    def test_no_extra_snapshot_keys_such_as_secrets_or_proxy_proofs(self):
        for name in ("envVars", "apiKey", "Authorization",
                     "raw_socket_peer_provider", "ingress_header_sanitization_proof"):
            with self.subTest(key=name):
                self.deny(snap=snapshot(**{name: "not-a-proof"}))

    def test_snapshot_missing_or_invalid_metadata_denied(self):
        for case in (
            {"service_name": "ouou-home-telegram"},
            snapshot(service_type="static_site"),
            snapshot(deployed_commit_sha="bad"),
            snapshot(auto_deploy_trigger="yes"),
            snapshot(deploy_status="running"),
            snapshot(branch="a\nb"),
            snapshot(start_command="python \runsafely"),
            snapshot(repository_full_name="example"),
        ):
            with self.subTest(case=tuple(case)):
                self.deny(snap=case)

    def test_review_flag_revocation_does_not_produce_result(self):
        os.environ["MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED"] = "False"
        self.deny()

    def test_cli_always_returns_blocked_nonzero_and_sanitized_json(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = checker.main([
                "--review-branch", _BRANCH, "--review-sha", _HEAD,
                "--blueprint", str(_ROOT / "render.yaml"),
            ])
        report = json.loads(stream.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "release_blocked")
        self.assertFalse(report["may_deploy"])
        self.assertIn("live_service_snapshot_missing", report["blockers"])
        self.assertNotIn(_HEAD, stream.getvalue())

    def test_cli_direct_script_launch_from_repo_root_is_blocked_safely(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(_ROOT / "scripts" / "memory_oidc_render_drift_check_offline.py"),
                "--blueprint", str(_ROOT / "render.yaml"),
                "--review-branch", _BRANCH, "--review-sha", _HEAD,
            ],
            cwd=_ROOT,
            env={**os.environ,
                 "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED": "true",
                 "OPENAI_API_KEY": ""},
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(completed.stderr, "")
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "release_blocked")
        self.assertFalse(report["may_deploy"])

    def test_cli_rejects_unknown_snapshot_fields_without_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "redacted.json"
            leaked = "DO_NOT_LOG_SYNTHETIC_SECRET"
            snapshot_path.write_text(
                json.dumps(snapshot(apiKey=leaked)), encoding="utf-8",
            )
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = checker.main([
                    "--review-branch", _BRANCH, "--review-sha", _HEAD,
                    "--blueprint", str(_ROOT / "render.yaml"),
                    "--snapshot", str(snapshot_path),
                ])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stream.getvalue())["status"],
                             "review_unavailable")
            self.assertNotIn(leaked, stream.getvalue())

    def test_no_network_or_login_and_memory_grant_in_contract(self):
        result = self.review()
        self.assertFalse(hasattr(result, "login"))
        self.assertFalse(hasattr(result, "remember"))
        self.assertFalse(hasattr(result, "approve_deploy"))
        self.assertGreaterEqual(len(result.blockers), 8)


if __name__ == "__main__":
    unittest.main()
