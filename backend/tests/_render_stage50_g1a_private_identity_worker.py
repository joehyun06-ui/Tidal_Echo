"""Synthetic-only Stage50 G1A privacy/identity gate tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import render_stage50_g1a_private_identity_readonly as g1a


class Stage50FakeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guiting-stage50-fake-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        names = (
            "codex-fake.db", "ledger-fake.sqlite3", "fake-backup.sqlite",
            *(f"anonymous-{i:02d}.db" for i in range(8))
        )
        for name in names:
            (self.root / name).write_bytes(b"FAKE_BYTES_NOT_AN_SQLITE_DB")
        for i in range(4):
            (self.root / f"secret-directory-{i}").mkdir()
        (self.root / "relay.db").write_bytes(b"KNOWN_FIXED_ROOT_NOT_INCLUDED")

    def test_private_name_hints_no_content_and_no_valid_sqlite_claim(self):
        record = g1a.identify_name_hints(self.root, synthetic=True)
        self.assertEqual(record["result"], "NAME_HINTS_ONLY")
        self.assertEqual(record["candidate_extensions_observed"], 11)
        self.assertEqual(record["unclassified_root_directories_observed"], 4)
        self.assertEqual(record["name_hint_counts"], {
            "codex_name_hint": 1, "ledger_name_hint": 1,
            "archive_name_hint": 1, "unclassified_name_hint": 8,
        })
        self.assertEqual(record["identity_confirmed_count"], 0)
        self.assertEqual(record["valid_sqlite_count"], "UNKNOWN_NOT_INSPECTED")
        self.assertFalse(record["sqlite_header_or_schema_checked"])
        self.assertFalse(record["content_or_secret_values_read"])
        self.assertFalse(record["raw_names_emitted"])
        self.assertFalse(record["real_files_exported"])
        self.assertFalse(record["source_writes"])
        self.assertEqual(record["backup_or_restore"], "NOT_AUTHORIZED_NOT_TESTED")
        payload = json.dumps(record)
        for word in ("codex-fake", "ledger-fake", "fake-backup", "anonymous-",
                     "secret-directory", "FAKE_BYTES", "KNOWN_FIXED"):
            self.assertNotIn(word, payload)

    def test_refuses_added_sqlite_candidate(self):
        (self.root / "unexpected.db").write_bytes(b"FAKE")
        with self.assertRaisesRegex(g1a.ScopeChanged, "count_drift"):
            g1a.identify_name_hints(self.root, synthetic=True)

    def test_refuses_removed_candidate(self):
        (self.root / "anonymous-00.db").unlink()
        with self.assertRaisesRegex(g1a.ScopeChanged, "count_drift"):
            g1a.identify_name_hints(self.root, synthetic=True)

    def test_refuses_unexpected_other_file_type(self):
        (self.root / "key.txt").write_bytes(b"FAKE")
        with self.assertRaisesRegex(g1a.ScopeChanged, "scope_type_or_name_drift"):
            g1a.identify_name_hints(self.root, synthetic=True)

    def test_refuses_symlink_candidate_without_following_target(self):
        db = self.root / "anonymous-00.db"
        db.unlink()
        try:
            db.symlink_to(self.root / "missing-sensitive-target")
        except OSError:
            self.skipTest("OS does not allow test symlinks")
        with self.assertRaisesRegex(g1a.ScopeChanged, "scope_type_or_name_drift"):
            g1a.identify_name_hints(self.root, synthetic=True)

    def test_double_scan_detects_metadata_drift(self):
        real_scan = g1a.scan_once
        calls = 0
        def scan(root, *, synthetic):
            nonlocal calls
            calls += 1
            result = real_scan(root, synthetic=synthetic)
            if calls == 2:
                return (result[0] + (("changed",),), result[1])
            return result
        with mock.patch.object(g1a, "scan_once", side_effect=scan):
            with self.assertRaisesRegex(g1a.ScopeChanged, "metadata_changed_during_audit"):
                g1a.identify_name_hints(self.root, synthetic=True)

    def test_requires_explicit_synthetic_mode(self):
        prev = os.environ.pop("GUITING_STAGE50_SYNTHETIC_ONLY", None)
        self.addCleanup(
            lambda: (os.environ.pop("GUITING_STAGE50_SYNTHETIC_ONLY", None),
                     os.environ.__setitem__("GUITING_STAGE50_SYNTHETIC_ONLY", prev))
            if prev is not None else os.environ.pop("GUITING_STAGE50_SYNTHETIC_ONLY", None)
        )
        args = ["stage50", "--perform-g1a-readonly", "--synthetic-root", str(self.root)]
        with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(g1a.main(), 2)
        self.assertIn("STOP_SCOPE_CHANGED_OR_UNAVAILABLE", output.getvalue())
        os.environ["GUITING_STAGE50_SYNTHETIC_ONLY"] = "true"
        with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(g1a.main(), 0)
        info = json.loads(output.getvalue())
        self.assertEqual(info["scope"], "SYNTHETIC_ONLY")
        self.assertNotIn("anonymous-00", output.getvalue())


if __name__ == "__main__":
    unittest.main()
