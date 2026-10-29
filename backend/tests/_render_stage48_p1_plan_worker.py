"""Stage48 pure in-memory approval planning tests: no Render/file/DB data."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import unittest
from unittest import mock

from scripts import render_stage48_p1_recovery_decision_offline as p


class Stage48RecoveryPolicyTests(unittest.TestCase):
    def test_metadata_only_does_not_certify_valid_sqlite_or_restore(self):
        result = p.evaluate(p.fixture())
        self.assertEqual(result["result"], "DESIGN_ONLY")
        self.assertEqual(result["unknown_root_extension_candidates"], 2)
        self.assertEqual(result["unknown_valid_sqlite_confirmed"], 0)
        self.assertEqual(result["unknown_valid_sqlite_confirmation"], "NOT_TESTED")
        self.assertEqual(result["known_codex_sqlite"], "PRESENT_UNVALIDATED")
        self.assertFalse(result["authorizes_export_or_backup"])
        self.assertFalse(result["any_database_opened"])
        self.assertEqual(result["real_p1_restore"], "NOT_TESTED")

    def test_rejects_arbitrary_names_paths_secret_values_and_extra_fields(self):
        for field in ("filenames", "paths", "key", "secret_value"):
            evidence = p.fixture()
            evidence[field] = "DONOTEMIT"
            with self.assertRaises(p.RejectedEvidence):
                p.evaluate(evidence)
        evidence = p.fixture()
        evidence["anonymous_unclassified_root"]["buckets"]["secret_key_name"] = 1
        with self.assertRaises(p.RejectedEvidence):
            p.evaluate(evidence)

    def test_rejects_inconsistent_aggregate_counts(self):
        evidence = p.fixture()
        evidence["anonymous_unclassified_root"]["buckets"]["sqlite_candidates"] = 11
        with self.assertRaisesRegex(p.RejectedEvidence, "anonymous_totals"):
            p.evaluate(evidence)

    def test_rejects_improper_read_or_mutation_evidence(self):
        for flag in ("contents_read", "environment_values_read",
                     "file_names_emitted", "source_writes",
                     "backup_created", "restoration_tested"):
            evidence = p.fixture()
            evidence[flag] = True
            with self.assertRaisesRegex(p.RejectedEvidence, "non_readonly"):
                p.evaluate(evidence)

    def test_partial_home_scan_never_promoted_to_complete(self):
        evidence = p.fixture()
        evidence["codex_home"]["truncated"] = True
        evidence["codex_home"]["stop_reason"] = "depth_limit"
        evidence["result"] = "PARTIAL_METADATA"
        outcome = p.evaluate(evidence)
        self.assertEqual(outcome["metadata"], "PARTIAL")
        self.assertEqual(outcome["real_p1_restore"], "NOT_TESTED")
        evidence["result"] = "PASS_METADATA"
        with self.assertRaises(p.RejectedEvidence):
            p.evaluate(evidence)

    def test_ledger_activation_not_inferred(self):
        evidence = p.fixture()
        evidence["usage_ledger_status"] = "enabled"
        with self.assertRaisesRegex(p.RejectedEvidence, "ledger_state_not_observed"):
            p.evaluate(evidence)

    def test_rejects_bool_as_file_count(self):
        evidence = p.fixture()
        evidence["anonymous_unclassified_root"]["total"] = True
        with self.assertRaisesRegex(p.RejectedEvidence, "invalid_nonnegative"):
            p.evaluate(evidence)

    def test_cli_refuses_without_synthetic_gate(self):
        previous = os.environ.pop("GUITING_STAGE48_SYNTHETIC_ONLY", None)
        self.addCleanup(lambda: (os.environ.pop("GUITING_STAGE48_SYNTHETIC_ONLY", None),
                                 os.environ.__setitem__("GUITING_STAGE48_SYNTHETIC_ONLY", previous))
                        if previous is not None else os.environ.pop("GUITING_STAGE48_SYNTHETIC_ONLY", None))
        with mock.patch("sys.argv", ["test", "--synthetic-only"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(p.main(), 2)
        self.assertIn("DENIED_NOT_SYNTHETIC", output.getvalue())
        os.environ["GUITING_STAGE48_SYNTHETIC_ONLY"] = "true"
        with mock.patch("sys.argv", ["test", "--synthetic-only"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(p.main(), 0)
        observed = json.loads(output.getvalue())
        self.assertFalse(observed["authorizes_export_or_backup"])
        self.assertEqual(observed["evidence_scope"], "SYNTHETIC_ONLY")


if __name__ == "__main__":
    unittest.main()
