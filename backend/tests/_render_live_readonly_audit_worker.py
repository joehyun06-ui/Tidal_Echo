"""Strictly offline contract tests for a historical Render live evidence pack.

These validate a bounded redacted record, not live Render, actual HTTP
availability, a secure TLS ingress or permission to roll back.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend.render_live_observation_review_offline import (
    ReadonlyRenderAuditUnavailable,
    classify_render_readonly_observation,
)
from backend.tests._support import NoNetworkMixin

ROOT = Path(__file__).resolve().parents[2]
FILE = ROOT / "docs" / "evidence" / "render-post169-readonly-20261028.json"
LIVE = "33f2fb9e09d42a901728355720b8334054c7fab5"
PREVIOUS = "a2d8699a7e2a0e9126d2ca565bfb3cd2297c181c"


def record():
    return json.loads(FILE.read_text(encoding="utf-8"))


class LiveRenderReadonlyAuditTests(NoNetworkMixin, unittest.TestCase):
    def review(self, snapshot=None, sha=LIVE):
        return classify_render_readonly_observation(
            record() if snapshot is None else snapshot,
            expected_live_sha=sha,
        )

    def deny(self, snapshot):
        with self.assertRaisesRegex(
            ReadonlyRenderAuditUnavailable,
            "render_live_readonly_audit_unavailable",
        ):
            self.review(snapshot)

    def test_archived_deployment_matches_exact_known_merge(self):
        r = record()
        self.assertEqual(r["live_sha"], LIVE)
        self.assertEqual(r["previous_sha"], PREVIOUS)
        self.assertEqual(r["live_deploy_id"], "dep-dbh0mv6tlv9c73dj16mg")
        self.assertEqual(r["previous_deploy_id"], "dep-dbdd6ld9fdbs73anfue0")
        self.assertEqual(r["live_trigger"], "api")

    def test_actual_snapshot_is_reviewable_but_never_authorized(self):
        d = self.review()
        self.assertTrue(d.live_sha_matches_expected)
        self.assertTrue(d.p3_start_verified_by_metadata_only)
        self.assertTrue(d.live_service_recorded)
        self.assertTrue(d.instance_recovered_after_switch_in_metric)
        self.assertTrue(d.observed_http_502)
        self.assertEqual(d.later_http_200_count, 7)
        for value in (
            d.may_deploy, d.may_rollback, d.may_enable_oidc,
            d.may_write_memory, d.may_use_paid_api,
        ):
            self.assertIs(value, False)

    def test_502_is_flagged_despite_later_200_samples(self):
        d = self.review()
        self.assertIn("post_deploy_502_requires_correlation", d.blockers)
        self.assertIn("missing_502_request_log_prevents_root_cause", d.blockers)
        self.assertIn("healthz_log_absence_is_not_success", d.blockers)
        self.assertIn("public_http_health_not_independently_verified",
                      d.blockers)

    def test_even_zero_502_and_no_event_failures_cannot_clear_blockers(self):
        r = record()
        r["http_metric_502_count"] = 0
        r["failure_event_count_in_filtered_window"] = 0
        d = self.review(r)
        self.assertFalse(d.observed_http_502)
        self.assertFalse(d.may_deploy)
        self.assertFalse(d.may_rollback)
        self.assertFalse(d.may_enable_oidc)
        self.assertIn("request_bound_tls_and_raw_peer_unattested", d.blockers)

    def test_new_live_commit_must_not_pass_old_snapshot_as_current(self):
        d = self.review(sha="f" * 40)
        self.assertFalse(d.live_sha_matches_expected)
        self.assertIn("observed_live_sha_differs_from_reviewed_sha",
                      d.blockers)
        self.assertFalse(d.may_deploy)

    def test_transcript_of_service_is_not_edge_attestation(self):
        d = self.review()
        for blocker in (
            "archived_metadata_not_a_live_recheck",
            "request_bound_tls_and_raw_peer_unattested",
            "forwarded_header_sanitization_unattested",
            "real_oidc_memory_and_paid_api_activation_not_checked",
        ):
            self.assertIn(blocker, d.blockers)

    def test_rollback_artifact_and_persistent_disk_are_unconfirmed(self):
        d = self.review()
        self.assertIn("durable_disk_snapshot_and_schema_rollback_not_checked",
                      d.blockers)
        self.assertIn("previous_deploy_artifact_retention_not_checked",
                      d.blockers)
        self.assertIn("rollback_not_user_authorized", d.blockers)

    def test_any_fabricated_release_permission_is_rejected(self):
        for field in (
            "audit_authorizes_deployment", "audit_authorizes_rollback",
            "audit_authorizes_oidc", "audit_authorizes_memory_write",
            "audit_authorizes_paid_api",
        ):
            with self.subTest(field=field):
                r = record()
                r[field] = True
                self.deny(r)

    def test_cannot_claim_live_activation_or_tls_proof(self):
        for field in (
            "public_http_health_response_verified",
            "production_edge_tls_peer_verified",
            "forwarding_header_sanitization_verified",
            "live_oidc_flags_checked",
            "live_memory_write_flags_checked",
            "paid_api_activation_checked",
            "disk_snapshot_and_compatibility_checked",
            "rollback_build_artifact_available_checked",
        ):
            with self.subTest(field=field):
                r = record()
                r[field] = True
                self.deny(r)

    def test_secret_fields_and_forged_edge_evidence_are_denied(self):
        for key in ("envVars", "api_key", "jwt", "raw_socket_peer_proof",
                    "edge_tls_signed_attestation", "user_data"):
            with self.subTest(key=key):
                r = record()
                r[key] = "SYNTHETIC_SECRET_DO_NOT_REPORT"
                self.deny(r)

    def test_missing_fields_and_bad_document_type_are_denied(self):
        r = record()
        del r["previous_sha"]
        self.deny(r)
        with self.assertRaises(ReadonlyRenderAuditUnavailable):
            self.review([])

    def test_spoofed_deploy_id_or_status_is_denied(self):
        for key, value in (
            ("live_deploy_id", "../dep-attacker"),
            ("previous_deploy_id", "dep-not-a-real-id"),
            ("live_status", "failed"),
            ("previous_status", "live"),
            ("deploy_end_event", "failed"),
        ):
            with self.subTest(key=key):
                r = record()
                r[key] = value
                self.deny(r)

    def test_p3_and_autodeploy_parity_required(self):
        for key, value in (
            ("service_start_command", "python scripts/render_start.py"),
            ("auto_deploy_trigger", "commit"),
            ("health_check_path", "/callback/oidc"),
            ("deployment_branch", "attacker/branch"),
        ):
            with self.subTest(key=key):
                r = record()
                r[key] = value
                self.deny(r)

    def test_event_category_list_has_no_unknown_strings(self):
        for values in ([], ["relay:started"], ["CF-Ray:SYNTHETIC_SECRET"]):
            with self.subTest(values=values):
                r = record()
                r["p3_supervisor_event_categories"] = values
                self.deny(r)

    def test_telemetry_type_coercion_refused(self):
        for value in (True, -1, 1.5, "1", None):
            with self.subTest(value=value):
                r = record()
                r["http_metric_502_count"] = value
                self.deny(r)

    def test_times_must_be_utc_and_ordered(self):
        for key, value in (
            ("deploy_started_utc", "2026-10-28T14:49:32"),
            ("deploy_finished_utc", "2026-10-27T14:51:19Z"),
            ("http_metric_200_bucket_utc", "2026-10-28T14:52:00Z"),
        ):
            with self.subTest(key=key):
                r = record()
                r[key] = value
                self.deny(r)

    def test_does_not_erase_502_when_request_logs_empty(self):
        d = self.review()
        self.assertTrue(d.observed_http_502)
        self.assertIn("missing_502_request_log_prevents_root_cause",
                      d.blockers)
        self.assertEqual(record()["request_502_log_count_in_filtered_window"], 0)

    def test_cli_always_returns_nonzero_and_redacted_blockers(self):
        run = subprocess.run([
            sys.executable,
            str(ROOT / "scripts" / "render_live_readonly_audit_check_offline.py"),
            "--snapshot", str(FILE),
            "--expected-live-sha", LIVE,
        ], cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(run.stderr, "")
        data = json.loads(run.stdout)
        self.assertEqual(data["status"], "release_and_rollback_blocked")
        self.assertTrue(data["observed_http_502"])
        self.assertFalse(data["may_rollback"])
        self.assertFalse(data["may_enable_oidc"])
        self.assertNotIn(LIVE, run.stdout)

    def test_cli_rejects_forged_secret_without_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "unsafe.json"
            r = record()
            r["PRIVATE_API_TOKEN"] = "SYNTHETIC_SECRET_DO_NOT_LOG"
            file.write_text(json.dumps(r), encoding="utf-8")
            run = subprocess.run([
                sys.executable,
                str(ROOT / "scripts" / "render_live_readonly_audit_check_offline.py"),
                "--snapshot", str(file),
                "--expected-live-sha", LIVE,
            ], cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(json.loads(run.stdout)["status"], "audit_unavailable")
            self.assertNotIn("SYNTHETIC_SECRET_DO_NOT_LOG",
                             run.stdout + run.stderr)

    def test_no_privileged_methods_or_service_calls(self):
        r = self.review()
        for method in ("rollback", "deploy", "enable_oidc", "remember",
                       "set_environment_variables"):
            self.assertFalse(hasattr(r, method))

    def test_repr_contains_no_repository_or_raw_deploy_identifier(self):
        self.assertNotIn(LIVE, repr(self.review()))
        self.assertNotIn("ouou-home-telegram", repr(self.review()))
        self.assertNotIn("33f2fb9e", repr(self.review()))


if __name__ == "__main__":
    unittest.main()
