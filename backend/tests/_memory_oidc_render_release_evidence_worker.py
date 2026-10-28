"""Read-only Render release evidence classifier regression (offline only).

Uses the publicly non-secret values previously observed through an authorized
read-only connector session. This fixture is NOT signed/render-authenticated
evidence for CI, and these tests never connect to Render.
"""
from __future__ import annotations

import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.memory_oidc_render_release_evidence_offline import (
    RenderEvidenceOfflineError,
    assess_render_release_evidence_offline,
)
from backend.tests._support import NoNetworkMixin

_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_BRANCH = "test/memory-oidc-pinned-origin-real-browser-offline-20261028"
_REVIEW_SHA = "efc4bd3108ee9e3a72ff181b387ec4df63344c78"
_OBSERVED_SHA = "a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c"


def blueprint():
    return json.loads((_ROOT / "render.yaml").read_text(encoding="utf-8"))


def redacted_record(**changes):
    """Non-secret metadata transcribed from read-only connector; not a proof."""
    values = {
        "service_name": "ouou-home-telegram",
        "repository": "joehyun06-ui/Tidal_Echo",
        "branch": "feat/render-telegram-deployment",
        "live_sha": _OBSERVED_SHA,
        "start_command": "python scripts/render_start_p3.py",
        "auto_deploy_trigger": "off",
        "render_subdomain_policy": "enabled",
        "ingress_ipv4_cidrs": ["0.0.0.0/0"],
        "health_check_path": "/healthz",
        "deploy_status": "live",
    }
    values.update(changes)
    return values


class RenderReleaseEvidenceTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {
            "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED": "true",
            "MEMORY_OIDC_REAL_NETWORK_EXPLICITLY_APPROVED": "false",
            "OPENAI_API_KEY": "",
        })
        p.start()
        self.addCleanup(p.stop)

    def assess(self, data=None, *, doc=None, branch=_REVIEW_BRANCH, sha=_REVIEW_SHA):
        return assess_render_release_evidence_offline(
            blueprint() if doc is None else doc,
            review_branch=branch, review_sha=sha,
            observed_metadata=data,
        )

    def reject(self, **kw):
        with self.assertRaisesRegex(
            RenderEvidenceOfflineError,
            "render_release_evidence_offline_unavailable",
        ):
            self.assess(**kw)

    def test_default_off_never_evaluates(self):
        os.environ.pop("MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED")
        self.reject(data=redacted_record())

    def test_missing_live_snapshot_fails_closed(self):
        report = self.assess()
        self.assertFalse(report.record_present_unverified)
        self.assertIsNone(report.deployed_sha_matches_review)
        self.assertIsNone(report.record_command_matches_p3)
        self.assertIn("live_metadata_record_missing", report.blockers)
        self.assertFalse(report.may_merge)
        self.assertFalse(report.may_deploy)
        self.assertFalse(report.may_enable_oidc)
        self.assertFalse(report.may_write_memory)

    def test_real_readonly_metadata_accurate_drift_classification(self):
        report = self.assess(redacted_record())
        self.assertTrue(report.record_present_unverified)
        self.assertFalse(report.blueprint_branch_matches_review)
        self.assertTrue(report.blueprint_autodeploy_off)
        self.assertFalse(report.blueprint_command_matches_actual_record)
        self.assertFalse(report.deployed_sha_matches_review)
        self.assertFalse(report.record_branch_matches_review)
        self.assertTrue(report.record_command_matches_p3)
        self.assertTrue(report.record_autodeploy_off)
        self.assertTrue(report.public_ipv4_unrestricted)
        self.assertTrue(report.default_subdomain_enabled)
        for blocker in (
            "live_commit_not_reviewed_sha", "live_branch_not_review_branch",
            "blueprint_command_differs_from_record",
            "public_edge_ipv4_unrestricted_not_direct_socket_proof",
            "default_onrender_host_alias_requires_policy",
        ):
            self.assertIn(blocker, report.blockers)

    def test_perfect_candidate_metadata_still_not_proven_authority(self):
        result = self.assess(redacted_record(
            branch=_REVIEW_BRANCH,
            live_sha=_REVIEW_SHA,
            start_command="python scripts/render_start.py",
            ingress_ipv4_cidrs=["203.0.113.0/24"],
            render_subdomain_policy="disabled",
        ))
        self.assertTrue(result.deployed_sha_matches_review)
        self.assertTrue(result.blueprint_command_matches_actual_record)
        self.assertFalse(result.public_ipv4_unrestricted)
        self.assertFalse(result.default_subdomain_enabled)
        for key in (
            "metadata_record_is_unverified_input",
            "per_request_ingress_peer_provenance_not_attested",
            "client_supplied_proxy_header_sanitization_not_attested",
            "real_edge_tls_termination_not_independently_attested",
            "real_idp_browser_session_and_consent_not_accepted",
            "draft_security_stack_not_merged_and_deployed",
        ):
            self.assertIn(key, result.blockers)
        self.assertFalse(result.may_deploy)
        self.assertFalse(result.may_enable_oidc)

    def test_render_public_ipv4_rule_is_not_direct_socket_proof(self):
        r = self.assess(redacted_record())
        self.assertIn("not_direct_socket_proof",
                      " ".join(r.blockers))
        self.assertIn("per_request_ingress_peer_provenance_not_attested",
                      r.blockers)

    def test_autodeploy_drift_is_separately_detected(self):
        result = self.assess(redacted_record(auto_deploy_trigger="commit"))
        self.assertFalse(result.record_autodeploy_off)
        self.assertIn("observed_auto_deploy_not_off", result.blockers)

    def test_blueprint_autodeploy_trigger_override_takes_precedence(self):
        doc = blueprint()
        doc["services"][0]["autoDeployTrigger"] = "commit"
        self.assertFalse(self.assess(doc=doc).blueprint_autodeploy_off)
        doc["services"][0]["autoDeployTrigger"] = "off"
        doc["services"][0]["autoDeploy"] = True
        self.assertTrue(self.assess(doc=doc).blueprint_autodeploy_off)

    def test_wrong_blueprint_target_fails(self):
        for service_list in ([], [{"name": "other", "type": "web"}]):
            with self.subTest(case=service_list):
                doc = blueprint()
                doc["services"] = service_list
                self.reject(doc=doc)

    def test_duplicate_blueprint_target_fails(self):
        doc = blueprint()
        doc["services"].append(dict(doc["services"][0]))
        self.reject(doc=doc)

    def test_changed_blueprint_start_command_flagged(self):
        doc = blueprint()
        doc["services"][0]["startCommand"] = "uvicorn backend.app:app"
        r = self.assess(doc=doc)
        self.assertIn("blueprint_start_command_changed", r.blockers)

    def test_changed_blueprint_branch_flagged(self):
        doc = blueprint()
        doc["services"][0]["branch"] = "feat/something-else"
        r = self.assess(doc=doc)
        self.assertFalse(r.blueprint_branch_matches_review)

    def test_missing_or_extra_snapshot_fields_rejected(self):
        record = redacted_record()
        del record["live_sha"]
        self.reject(data=record)
        self.reject(data=redacted_record(api_key="not-a-real-key"))
        self.reject(data=redacted_record(envVars="do-not-accept"))
        self.reject(data=redacted_record(raw_socket_peer_proof=True))
        self.reject(data=redacted_record(edge_tls_proof=True))

    def test_never_accepts_credentials_or_nested_secret_values(self):
        self.reject(data=redacted_record(repository={"token": "fake"}))
        self.reject(data=redacted_record(start_command="run\nsecret"))
        self.reject(data=redacted_record(branch="feat/\x00bad"))

    def test_bad_review_branch_and_sha_rejected(self):
        for branch in ("", "/bad", "a//b", "a..b", "bad branch"):
            with self.subTest(branch=branch):
                self.reject(branch=branch)
        for sha in ("a" * 39, "A" * 40, "g" * 40):
            with self.subTest(sha=sha):
                self.reject(sha=sha)

    def test_invalid_cidr_ranges_rejected(self):
        for cidr in (
            "999.0.0.0/0", "0.0.0.0/99", "0.0.0.0/-1",
            "0.0.0.00/0", "2001:db8::/32", "not-ip",
        ):
            with self.subTest(cidr=cidr):
                self.reject(data=redacted_record(
                    ingress_ipv4_cidrs=[cidr],
                ))

    def test_duplicate_and_empty_cidr_lists_rejected(self):
        for raw in ([], ["0.0.0.0/0"] * 2, ["0.0.0.0/0"] * 9):
            with self.subTest(raw=raw):
                self.reject(data=redacted_record(
                    ingress_ipv4_cidrs=raw,
                ))

    def test_wrong_service_identity_rejected(self):
        for change in (
            {"service_name": "other"},
            {"repository": "attacker/elsewhere"},
            {"health_check_path": "/auth/login"},
            {"deploy_status": "running"},
            {"auto_deploy_trigger": "always"},
            {"render_subdomain_policy": "unknown"},
        ):
            with self.subTest(change=change):
                self.reject(data=redacted_record(**change))

    def test_not_live_deployment_keeps_release_blocked(self):
        r = self.assess(redacted_record(deploy_status="not_live"))
        self.assertIn("observed_deployment_not_live", r.blockers)

    def test_missing_expected_p3_command_is_distinct_blocker(self):
        r = self.assess(redacted_record(start_command="python scripts/render_start.py"))
        self.assertFalse(r.record_command_matches_p3)
        self.assertIn("observed_p3_entrypoint_not_confirmed", r.blockers)

    def test_result_repr_never_contains_sha_or_service_metadata(self):
        r = self.assess(redacted_record())
        self.assertNotIn(_OBSERVED_SHA, repr(r))
        self.assertNotIn("ouou-home-telegram", repr(r))
        self.assertNotIn("joehyun06", repr(r))

    def test_all_permission_fields_constantly_false(self):
        r = self.assess(redacted_record(
            branch=_REVIEW_BRANCH,
            live_sha=_REVIEW_SHA,
        ))
        self.assertFalse(r.may_merge)
        self.assertFalse(r.may_deploy)
        self.assertFalse(r.may_enable_oidc)
        self.assertFalse(r.may_write_memory)

    def test_review_flag_revocation_denies_without_exception_data(self):
        os.environ["MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED"] = "false"
        self.reject(data=redacted_record())

    def test_standalone_cli_returns_blocked_nonzero_for_real_blueprint(self):
        process = subprocess.run([
            sys.executable,
            str(_ROOT / "scripts" / "memory_oidc_render_release_evidence_check_offline.py"),
            "--blueprint", str(_ROOT / "render.yaml"),
            "--review-branch", _REVIEW_BRANCH,
            "--review-sha", _REVIEW_SHA,
        ], cwd=_ROOT, env={**os.environ,
            "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED": "true"},
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(process.returncode, 2, process.stderr)
        self.assertEqual(process.stderr, "")
        report = json.loads(process.stdout)
        self.assertEqual(report["status"], "release_blocked")
        self.assertFalse(report["may_deploy"])
        self.assertFalse(report["may_enable_oidc"])
        self.assertIn("live_metadata_record_missing", report["blockers"])
        self.assertNotIn(_REVIEW_SHA, process.stdout)

    def test_standalone_cli_rejects_secret_key_and_never_echoes_value(self):
        with tempfile.TemporaryDirectory() as dirname:
            path = Path(dirname) / "no-real-credentials.json"
            sensitive_fixture = "SYNTHETIC_SECRET_DO_NOT_LOG"
            path.write_text(json.dumps(redacted_record(
                api_key=sensitive_fixture,
            )), encoding="utf-8")
            result = subprocess.run([
                sys.executable,
                str(_ROOT / "scripts" / "memory_oidc_render_release_evidence_check_offline.py"),
                "--blueprint", str(_ROOT / "render.yaml"),
                "--observed", str(path),
                "--review-branch", _REVIEW_BRANCH,
                "--review-sha", _REVIEW_SHA,
            ], cwd=_ROOT, env={**os.environ,
                "MEMORY_OIDC_RENDER_DRIFT_REVIEW_ENABLED": "true"},
                capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["status"],
                             "review_unavailable")
            self.assertNotIn(sensitive_fixture, result.stdout + result.stderr)

    def test_no_endpoint_session_or_mutation_methods(self):
        r = self.assess(redacted_record())
        self.assertFalse(hasattr(r, "deploy"))
        self.assertFalse(hasattr(r, "login"))
        self.assertFalse(hasattr(r, "remember"))


if __name__ == "__main__":
    unittest.main()
