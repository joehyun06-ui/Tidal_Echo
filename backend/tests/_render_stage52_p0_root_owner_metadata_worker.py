"""Stage52 P0 synthetic-only tests: no candidate or directory access."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import render_stage52_p0_root_owner_metadata_readonly as p0


class Stage52P0Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="guiting-stage52-fake-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        # Set up sensitive-looking decoys to show they are never visited.
        (self.root / "private-sample.sqlite3").write_bytes(b"DO_NOT_READ_PRIVATE")
        (self.root / "private-directory").mkdir()
        self.owner = self.root.lstat().st_uid

    def test_same_owner_relation(self):
        result = p0.audit_p0(self.root, synthetic=True, synthetic_euid=self.owner)
        self.assertEqual(result["root_owner_relation"], "MATCH")
        self.assertEqual(result["candidate_count"], "UNKNOWN_NOT_ENUMERATED")
        self.assertIsNone(result["candidate_owner_counts"])
        self.assertFalse(result["root_directory_opened"])
        self.assertFalse(result["root_directory_enumerated"])
        self.assertFalse(result["filesystem_noatime_verified"])
        self.assertFalse(result["g1b1_retried"])
        self.assertEqual(result["candidate_file_opens"], 0)
        self.assertEqual(result["candidate_content_bytes_read"], 0)

    def test_different_owner_relation(self):
        result = p0.audit_p0(self.root, synthetic=True, synthetic_euid=self.owner + 1)
        self.assertEqual(result["root_owner_relation"], "MISMATCH")

    def test_non_directory_stop(self):
        target = self.root / "private-sample.sqlite3"
        with self.assertRaises(p0.ScopeChanged):
            p0.audit_p0(target, synthetic=True, synthetic_euid=self.owner)

    def test_missing_root_stop(self):
        with self.assertRaises(OSError):
            p0.audit_p0(self.root / "nonexistent", synthetic=True, synthetic_euid=0)

    def test_symlink_root_stop(self):
        link = Path(self.tmp.name) / "link"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("runner disallows symlinks")
        with self.assertRaises(p0.ScopeChanged):
            p0.audit_p0(link, synthetic=True, synthetic_euid=self.owner)

    def test_metadata_drift_stop(self):
        orig = p0.snapshot_root
        called = 0
        def changing(path):
            nonlocal called
            called += 1
            s = orig(path)
            return s if called == 1 else s[:-1] + (s[-1] + 1,)
        with mock.patch.object(p0, "snapshot_root", side_effect=changing):
            with self.assertRaises(p0.ScopeChanged):
                p0.audit_p0(self.root, synthetic=True, synthetic_euid=self.owner)

    def test_zero_directory_open_enumerate_file_read_even_on_success(self):
        with (mock.patch.object(p0.os, "open", side_effect=AssertionError("opened filesystem")),
              mock.patch.object(p0.os, "scandir", side_effect=AssertionError("enumerated filesystem")),
              mock.patch.object(p0.os, "listdir", side_effect=AssertionError("enumerated filesystem")),
              mock.patch.object(Path, "open", side_effect=AssertionError("opened file")),
              mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read file"))):
            record = p0.audit_p0(self.root, synthetic=True, synthetic_euid=self.owner)
        self.assertEqual(record["result"], "ROOT_OWNER_RELATION_HINT_ONLY")

    def test_no_private_values_in_output(self):
        result = p0.audit_p0(self.root, synthetic=True, synthetic_euid=self.owner)
        record = json.dumps(result)
        for sensitive in ("private-sample", "DO_NOT_READ_PRIVATE", str(self.root),
                          "private-directory", "st_uid", "st_atime_ns", "CAP_FOWNER"):
            self.assertNotIn(sensitive, record)

    def test_synthetic_not_allowed_to_target_production(self):
        with self.assertRaises(p0.ScopeChanged):
            p0.audit_p0(p0.ROOT, synthetic=True, synthetic_euid=0)

    def test_requires_synthetic_gate_cli(self):
        args = ["stage52", "--perform-stage52-p0", "--owner-authorized-stage52-p0",
                "--synthetic-root", str(self.root), "--synthetic-euid", str(self.owner)]
        with mock.patch.dict(os.environ, {"GUITING_STAGE52_SYNTHETIC_ONLY": ""}):
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(p0.main(), 2)
            self.assertIn("STOP_SCOPE_OR_ACCESS_UNVERIFIED", out.getvalue())
        with mock.patch.dict(os.environ, {"GUITING_STAGE52_SYNTHETIC_ONLY": "true"}):
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(p0.main(), 0)
            self.assertEqual(json.loads(out.getvalue())["scope"], "SYNTHETIC_ONLY")

    def test_without_required_owner_flag_cli_fail(self):
        args = ["stage52", "--perform-stage52-p0"]
        with mock.patch("sys.argv", args), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                p0.main()
        self.assertNotEqual(cm.exception.code, 0)

    def test_cli_cannot_supply_synthetic_uid_to_production(self):
        args = ["stage52", "--perform-stage52-p0",
                "--owner-authorized-stage52-p0", "--synthetic-euid", "0"]
        with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(p0.main(), 2)
        self.assertIn("STOP_SCOPE_OR_ACCESS_UNVERIFIED", out.getvalue())

    def test_production_root_and_platform_validation(self):
        with mock.patch.object(p0, "ROOT", self.root):
            with mock.patch.object(p0.sys, "platform", "win32"):
                with self.assertRaises(p0.ScopeChanged):
                    p0.audit_p0(self.root, synthetic=False)

    def test_no_database_engine_privilege_or_network_calls(self):
        text = Path(p0.__file__).read_text("utf-8")
        for phrase in ("import sqlite3", "import socket", "import requests",
                       "subprocess", "os.chmod(", "os.chown(", "os.pread(",
                       "os.scandir(", "os.listdir("):
            self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
