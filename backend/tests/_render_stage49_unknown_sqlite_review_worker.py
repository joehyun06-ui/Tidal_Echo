"""Stage49 pure data-free phased approval rehearsal (mock aggregates only)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest import mock

from scripts import render_stage49_unknown_sqlite_review_design_offline as p


class Stage49FakeApprovalGates(unittest.TestCase):
    def setUp(self):
        self.fake = p.synthetic_fixture()

    def test_fixture_does_not_authorize_live_identity_or_transfer(self):
        result = p.design_only(self.fake)
        self.assertEqual(result["result"], "DESIGN_ONLY")
        self.assertEqual(result["candidate_extension_count"], 11)
        self.assertEqual(result["actual_valid_sqlite_count"], "UNKNOWN_NOT_INSPECTED")
        self.assertEqual(result["usage_ledger_identity"], "UNKNOWN_NOT_INSPECTED")
        self.assertFalse(result["real_file_names_read"])
        self.assertFalse(result["real_p1_contents_read"])
        self.assertFalse(result["production_transfer_allowed"])
        self.assertEqual(result["production_authorization"], "NONE")
        self.assertEqual(result["production_recovery_status"], "NOT_TESTED")

    def test_refuses_any_raw_names_paths_hashes_or_key_material(self):
        for field in ("filename", "path", "sha256", "api_key", "schema", "file_id"):
            entry = dict(self.fake)
            entry[field] = "private-name"
            with self.subTest(field=field), self.assertRaises(p.DeniedEvidence):
                p.design_only(entry)

    def test_refuses_non_synthetic_scope(self):
        entry = dict(self.fake)
        entry["scope"] = "LIVE_READONLY_METADATA"
        with self.assertRaisesRegex(p.DeniedEvidence, "real_data_not_accepted"):
            p.design_only(entry)

    def test_refuses_number_of_valid_dbs_above_readonly_header_review(self):
        entry = dict(self.fake)
        entry["sqlite_validated_count"] = 4
        with self.assertRaisesRegex(p.DeniedEvidence, "stage_gates_out_of_order"):
            p.design_only(entry)

    def test_refuses_backups_without_validated_db(self):
        entry = dict(self.fake)
        entry["isolated_backup_verified_count"] = 3
        with self.assertRaisesRegex(p.DeniedEvidence, "stage_gates_out_of_order"):
            p.design_only(entry)

    def test_refuses_restores_without_earlier_copy_verification(self):
        entry = dict(self.fake)
        entry["isolated_restore_verified_count"] = 2
        with self.assertRaisesRegex(p.DeniedEvidence, "stage_gates_out_of_order"):
            p.design_only(entry)

    def test_refuses_inconsistent_or_boolean_counts(self):
        for field in ("identity_review_count", "unknown_or_blocked_count"):
            entry = dict(self.fake)
            entry[field] = True
            with self.assertRaisesRegex(p.DeniedEvidence, "invalid_count"):
                p.design_only(entry)
        entry = dict(self.fake)
        entry["unknown_or_blocked_count"] = 6
        with self.assertRaisesRegex(p.DeniedEvidence, "unreviewed_count_inconsistent"):
            p.design_only(entry)

    def test_refuses_without_explicit_fake_data_gate(self):
        prior = os.environ.pop("GUITING_STAGE49_SYNTHETIC_ONLY", None)
        self.addCleanup(
            lambda: (os.environ.pop("GUITING_STAGE49_SYNTHETIC_ONLY", None),
                     os.environ.__setitem__("GUITING_STAGE49_SYNTHETIC_ONLY", prior))
            if prior is not None else os.environ.pop("GUITING_STAGE49_SYNTHETIC_ONLY", None)
        )
        argv = ["stage49", "--synthetic-only"]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(p.main(), 2)
        self.assertIn("DENIED_WITHOUT_SYNTHETIC_GATE", out.getvalue())
        os.environ["GUITING_STAGE49_SYNTHETIC_ONLY"] = "true"
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(p.main(), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["scope"], "SYNTHETIC_ONLY")
        self.assertFalse(result["production_transfer_allowed"])


if __name__ == "__main__":
    unittest.main()
