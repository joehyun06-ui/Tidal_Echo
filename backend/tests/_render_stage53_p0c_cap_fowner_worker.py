"""Stage53 P0C fake capability masks only; no live query inside CI."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from scripts import render_stage53_p0c_cap_fowner_readonly as p0c


class FakeCapTests(unittest.TestCase):
    def test_fake_effective_bit_present(self):
        self.assertEqual(p0c.report(synthetic_mask=1 << 3)["cap_fowner_effective"], "YES")

    def test_fake_effective_bit_absent(self):
        self.assertEqual(p0c.report(synthetic_mask=0)["cap_fowner_effective"], "NO")

    def test_unrelated_capabilities_do_not_count(self):
        self.assertEqual(p0c.report(synthetic_mask=1 << 20)["cap_fowner_effective"], "NO")

    def test_multiple_effective_bits_only_fowner_matters(self):
        self.assertEqual(p0c.report(synthetic_mask=(1 << 20) | (1 << 3))["cap_fowner_effective"], "YES")

    def test_negative_mask_refused(self):
        with self.assertRaises(p0c.StopCheck):
            p0c.mask_to_hint(-1)

    def test_above_32bit_mask_refused(self):
        with self.assertRaises(p0c.StopCheck):
            p0c.mask_to_hint(1 << 40)

    def test_invalid_mask_type_refused(self):
        with self.assertRaises(p0c.StopCheck):
            p0c.mask_to_hint("private-cap-mask")

    def test_only_aggregate_boolean_output(self):
        r = p0c.report(synthetic_mask=1 << 3)
        self.assertEqual(r["filesystem_objects_inspected"], 0)
        self.assertEqual(r["directory_opens"], 0)
        self.assertEqual(r["candidate_file_opens"], 0)
        self.assertEqual(r["candidate_content_bytes_read"], 0)
        self.assertFalse(r["g1b1_retried"])
        for s in ("/proc", "/var/data", "private-cap-mask", "CapEff", "uid=", "gid="):
            self.assertNotIn(s, json.dumps(r))

    def test_gated_synthetic_cli(self):
        args = ["p0c", "--perform-stage53-p0c", "--owner-authorized-stage53-p0c",
                "--synthetic-effective-mask", "8"]
        with mock.patch.dict(os.environ, {"GUITING_STAGE53_SYNTHETIC_ONLY": ""}):
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(p0c.main(), 2)
            self.assertIn("STOP_CAPABILITY_CHECK_UNAVAILABLE", out.getvalue())
        with mock.patch.dict(os.environ, {"GUITING_STAGE53_SYNTHETIC_ONLY": "true"}):
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(p0c.main(), 0)
            self.assertEqual(json.loads(out.getvalue())["scope"], "SYNTHETIC_ONLY")

    def test_explicit_authorization_flags_required(self):
        with mock.patch("sys.argv", ["p0c", "--perform-stage53-p0c"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                p0c.main()
        self.assertNotEqual(cm.exception.code, 0)

    def test_no_directory_or_file_access_in_synthetic_mode(self):
        with mock.patch.object(p0c.os, "open", side_effect=AssertionError("open")), \
             mock.patch.object(p0c.os, "stat", side_effect=AssertionError("stat")), \
             mock.patch.object(p0c.os, "listdir", side_effect=AssertionError("listdir")), \
             mock.patch.object(p0c.os, "scandir", side_effect=AssertionError("scandir")):
            self.assertEqual(p0c.report(synthetic_mask=0)["cap_fowner_effective"], "NO")

    def test_nonlinux_production_fails_without_syscall(self):
        with mock.patch.object(p0c.sys, "platform", "win32"), \
             mock.patch.object(p0c.ctypes, "CDLL", side_effect=AssertionError("no libc")):
            with self.assertRaises(p0c.StopCheck):
                p0c.current_cap_fowner_effective()

    @unittest.skipUnless(sys.platform == "linux", "Linux ABI only")
    def test_unknown_architecture_stops(self):
        with mock.patch.object(p0c.os, "uname", return_value=type("U", (), {"machine": "aarch64"})()), \
             mock.patch.object(p0c.ctypes, "CDLL", side_effect=AssertionError("no libc")):
            with self.assertRaises(p0c.StopCheck):
                p0c.current_cap_fowner_effective()

    @unittest.skipUnless(sys.platform == "linux", "Linux ABI only")
    def test_fake_syscall_success(self):
        numbers = []
        def fake_syscall(number, header_ptr, data_ptr):
            numbers.append(number.value)
            header = p0c.ctypes.cast(header_ptr, p0c.ctypes.POINTER(p0c.CapHeader)).contents
            self.assertEqual((header.version, header.pid), (p0c.LINUX_CAPABILITY_VERSION_3, 0))
            d = p0c.ctypes.cast(data_ptr, p0c.ctypes.POINTER(p0c.CapData * 2)).contents
            d[0].effective = 8
            d[0].permitted = 0
            return 0
        class FakeLib:
            syscall = staticmethod(fake_syscall)
        with mock.patch.object(p0c.ctypes, "CDLL", return_value=FakeLib()):
            self.assertEqual(p0c.current_cap_fowner_effective(), "YES")
        self.assertEqual(numbers, [p0c.SYS_CAPGET_X86_64])

    @unittest.skipUnless(sys.platform == "linux", "Linux ABI only")
    def test_failed_syscall_stops(self):
        class FakeLib:
            syscall = staticmethod(lambda *args: -1)
        with mock.patch.object(p0c.ctypes, "CDLL", return_value=FakeLib()):
            with self.assertRaises(p0c.StopCheck):
                p0c.current_cap_fowner_effective()

    @unittest.skipUnless(sys.platform == "linux", "Linux ABI only")
    def test_wrong_version_stops(self):
        def fake_syscall(number, header_ptr, data_ptr):
            header = p0c.ctypes.cast(header_ptr, p0c.ctypes.POINTER(p0c.CapHeader)).contents
            header.version = 0
            return 0
        class FakeLib:
            syscall = staticmethod(fake_syscall)
        with mock.patch.object(p0c.ctypes, "CDLL", return_value=FakeLib()):
            with self.assertRaises(p0c.StopCheck):
                p0c.current_cap_fowner_effective()


    @unittest.skipUnless(sys.platform == "linux", "Linux ABI only")
    def test_ctypes_argument_error_is_anonymized(self):
        class FakeLib:
            syscall = staticmethod(lambda *args: (_ for _ in ()).throw(p0c.ctypes.ArgumentError("PRIVATE_RAW_CAPABILITY")))
        with mock.patch.object(p0c.ctypes, "CDLL", return_value=FakeLib()):
            with self.assertRaises(p0c.StopCheck) as cm:
                p0c.current_cap_fowner_effective()
        self.assertNotIn("PRIVATE_RAW", str(cm.exception))

    def test_no_privilege_file_or_network_apis_in_source(self):
        from pathlib import Path
        s = Path(p0c.__file__).read_text("utf-8")
        for item in ("os.open(", "os.scandir(", "os.listdir(", "os.stat(",
                     "os.chmod(", "os.chown(", "capset(", "prctl(",
                     "sqlite3.connect", "subprocess.run", "syscall(SYS_capset"):
            self.assertNotIn(item, s)


if __name__ == "__main__":
    unittest.main()
