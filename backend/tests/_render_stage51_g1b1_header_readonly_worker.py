"""Stage51 G1B1 fake-only security/behavior tests. Never touch production."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

from scripts import render_stage50_g1a_private_identity_readonly as g1a
from scripts import render_stage51_g1b1_header_readonly as g1b


def header(*, page_size: int = 4096, read_version: int = 1, write_version: int = 1):
    data = bytearray(100)
    data[:16] = b"SQLite format 3\x00"
    data[16:18] = (1 if page_size == 65536 else page_size).to_bytes(2, "big")
    data[18] = write_version
    data[19] = read_version
    data[20] = 0
    data[21:24] = bytes((64, 32, 32))
    return bytes(data)


class G1B1SyntheticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guiting-stage51-fake-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.names = [f"secret-fixture-{i:02d}.sqlite3" for i in range(11)]
        for name in self.names:
            (self.root / name).write_bytes(header() + b"FAKE_PRIVATE_SUFFIX_NEVER_READ")
        (self.root / self.names[1]).write_bytes(b"NOT_SQLITE_MAGIC" + b"X" * 105)
        (self.root / self.names[2]).write_bytes(b"SQLite format 3\x00")
        for i in range(4):
            (self.root / f"private-fixture-folder-{i}").mkdir()
        (self.root / "relay.db").write_bytes(b"FIXED_NOT_IN_SCOPE")
        (self.root / "codex-generation.db").write_bytes(b"FIXED_NOT_IN_SCOPE")

    def scan(self):
        return g1b.audit_g1b1(self.root, synthetic=True)

    def test_aggregate_counts_without_identity_claim_or_sensitive_output(self):
        report = self.scan()
        self.assertEqual(report["candidate_count"], 11)
        self.assertEqual(report["header_compatible_count"], 9)
        self.assertEqual(report["header_nonmatching_count"], 1)
        self.assertEqual(report["header_insufficient_or_unsupported_count"], 1)
        self.assertEqual(report["identity_confirmed_count"], 0)
        self.assertEqual(report["valid_sqlite_count"], "UNKNOWN_NOT_VERIFIED")
        self.assertFalse(report["schema_checked"])
        self.assertFalse(report["database_engine_opened"])
        self.assertFalse(report["real_files_exported"])
        self.assertFalse(report["source_writes"])
        payload = json.dumps(report)
        for private_word in ("secret-fixture", "private-fixture", "FAKE_PRIVATE",
                             "codex-generation", "relay.db", "SQLite format"):
            self.assertNotIn(private_word, payload)

    def test_stage50_frozen_root_scope_parity(self):
        original, hints = g1a.scan_once(self.root, synthetic=True)
        same = g1b.scan_once(self.root, synthetic=True)
        self.assertEqual(original, same)
        self.assertEqual(sum(hints.values()), 11)

    def test_page_size_65536_and_512_supported(self):
        self.assertEqual(g1b.classify_header(header(page_size=65536)), "header_compatible_count")
        self.assertEqual(g1b.classify_header(header(page_size=512)), "header_compatible_count")

    def test_reject_unsupported_read_write_version(self):
        self.assertEqual(g1b.classify_header(header(read_version=3)), "header_insufficient_or_unsupported_count")
        self.assertEqual(g1b.classify_header(header(write_version=3)), "header_insufficient_or_unsupported_count")

    def test_reject_invalid_page_size_and_fraction(self):
        self.assertEqual(g1b.classify_header(header(page_size=1234)), "header_insufficient_or_unsupported_count")
        data = bytearray(header())
        data[21] = 63
        self.assertEqual(g1b.classify_header(bytes(data)), "header_insufficient_or_unsupported_count")
        data = bytearray(header(page_size=512))
        data[20] = 33
        self.assertEqual(g1b.classify_header(bytes(data)), "header_insufficient_or_unsupported_count")

    def test_short_and_wrong_magic(self):
        self.assertEqual(g1b.classify_header(b"SQLite format 3\x00"), "header_insufficient_or_unsupported_count")
        self.assertEqual(g1b.classify_header(b"NOT_SQLITE" + b"X" * 100), "header_nonmatching_count")

    def test_changed_candidate_count_fails_closed(self):
        (self.root / "extra-suspect.db").write_bytes(header())
        with self.assertRaises(g1b.ScopeChanged):
            self.scan()

    def test_missing_candidate_fails_closed(self):
        (self.root / self.names[0]).unlink()
        with self.assertRaises(g1b.ScopeChanged):
            self.scan()

    def test_changed_dir_count_fails_closed(self):
        (self.root / "unexpected-folder").mkdir()
        with self.assertRaises(g1b.ScopeChanged):
            self.scan()

    def test_other_unexpected_file_fails_closed(self):
        (self.root / "new-secret.txt").write_bytes(b"DO_NOT_READ")
        with self.assertRaises(g1b.ScopeChanged):
            self.scan()

    def test_symlink_file_fails_closed(self):
        p = self.root / self.names[0]
        p.unlink()
        try:
            p.symlink_to(self.root / "not-existent-secret-target")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this runner")
        with self.assertRaises(g1b.ScopeChanged):
            self.scan()

    @unittest.skipIf(os.name == "nt", "Windows fixture nlink differs; G1A synthetic guard is OS-specific")
    def test_hardlink_candidate_fails_closed(self):
        p = self.root / self.names[0]
        os.link(p, self.root / "second-hardlink-other-path")
        with self.assertRaises(g1b.ScopeChanged):
            self.scan()

    def test_detect_two_scan_metadata_drift(self):
        actual = g1b.scan_once
        n = 0
        def fake_scan(root, *, synthetic):
            nonlocal n
            n += 1
            record = actual(root, synthetic=synthetic)
            return record if n == 1 else record + (("drift",),)
        with mock.patch.object(g1b, "scan_once", side_effect=fake_scan):
            with self.assertRaises(g1b.ScopeChanged):
                self.scan()

    def test_detect_read_time_metadata_drift(self):
        original = g1b.checked_file_header
        n = 0
        def drift(*args, **kwargs):
            nonlocal n
            data = original(*args, **kwargs)
            n += 1
            if n == 1:
                (self.root / self.names[0]).write_bytes(header() + b"CHANGED_SUFFIX")
            return data
        with mock.patch.object(g1b, "checked_file_header", side_effect=drift):
            with self.assertRaises(g1b.ScopeChanged):
                self.scan()

    def test_explicit_authorization_and_synthetic_gate(self):
        args = ["stage51", "--perform-g1b1-header-only", "--owner-authorized-g1b1",
                "--synthetic-root", str(self.root)]
        with mock.patch.dict(os.environ, {"GUITING_STAGE51_SYNTHETIC_ONLY": ""}):
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(g1b.main(), 2)
            self.assertIn("STOP_SCOPE_CHANGED_OR_UNAVAILABLE", output.getvalue())
            self.assertNotIn("secret-fixture", output.getvalue())
        with mock.patch.dict(os.environ, {"GUITING_STAGE51_SYNTHETIC_ONLY": "true"}):
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(g1b.main(), 0)
            self.assertEqual(json.loads(output.getvalue())["scope"], "SYNTHETIC_ONLY")
            self.assertNotIn("secret-fixture", output.getvalue())

    def test_synthetic_invalid_root_refused(self):
        outside = self.root / "not-found"
        with self.assertRaises((OSError, g1b.ScopeChanged)):
            g1b.audit_g1b1(outside, synthetic=True)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux production POSIX behavior only")
    def test_linux_fake_root_prod_branch_noatime_bounded_reads(self):
        # Fake temporary root patched only inside this unit test. No Render data.
        observed = []
        real_pread = os.pread
        def counted_pread(fd, amount, offset):
            observed.append((amount, offset))
            return real_pread(fd, amount, offset)
        before = {name: (self.root / name).stat().st_atime_ns for name in self.names}
        with mock.patch.object(g1b, "ROOT", self.root), mock.patch.object(g1b.os, "pread", side_effect=counted_pread):
            report = g1b.audit_g1b1(self.root, synthetic=False)
        after = {name: (self.root / name).stat().st_atime_ns for name in self.names}
        self.assertEqual(before, after)
        self.assertEqual(report["header_compatible_count"], 9)
        self.assertEqual(observed, [(100, 0)] * 11)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux O_NOATIME gate only")
    def test_linux_noatime_denial_has_no_fallback(self):
        with mock.patch.object(g1b, "ROOT", self.root), mock.patch.object(
                g1b.os, "open", side_effect=PermissionError("private path must not be emitted")) as op:
            with self.assertRaises(g1b.ReadNotSafe):
                g1b.audit_g1b1(self.root, synthetic=False)
            self.assertEqual(op.call_count, 1)
            self.assertTrue(op.call_args.args[1] & os.O_NOATIME)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux production guard only")
    def test_linux_candidate_open_denial_no_retry(self):
        original = os.open
        calls = []
        def no_candidate_open(path, flags, *args, **kwargs):
            calls.append(flags)
            if "dir_fd" in kwargs:
                raise PermissionError("private path must not be emitted")
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(g1b, "ROOT", self.root), mock.patch.object(g1b.os, "open", side_effect=no_candidate_open):
            with self.assertRaises(g1b.ReadNotSafe):
                g1b.audit_g1b1(self.root, synthetic=False)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(flags & os.O_NOATIME for flags in calls))

    def test_does_not_import_database_engine_or_network(self):
        path = Path(g1b.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import sqlite3", path)
        self.assertNotIn("import socket", path)
        self.assertNotIn("import requests", path)
        self.assertNotIn("subprocess", path)
        self.assertIn("os.pread(fd, HEADER_LENGTH, 0)", path)


if __name__ == "__main__":
    unittest.main()
