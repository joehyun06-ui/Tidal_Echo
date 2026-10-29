"""Stage47 privacy-preserving P1 inventory unit tests: fake files only."""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import render_stage47_p1_metadata_readonly as probe


class Stage47SyntheticP1(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stage47-fake-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "codex-generation.db").write_bytes(b"FAKE SQLITE PLACEHOLDER")
        (self.root / "codex-home").mkdir()
        (self.root / "codex-home" / "private-token.txt").write_text(
            "NEVER_PUBLISH_THIS_VALUE", encoding="utf-8")
        sub = self.root / "codex-home" / "deep"
        sub.mkdir()
        (sub / "session-history.json").write_bytes(b"FAKE-SESSION-CONTENT")
        (self.root / "not-reviewed.db").write_bytes(b"FAKE")
        (self.root / "private-credential.pem").write_bytes(b"FAKE-SECRET")
        (self.root / "unreviewed-config.json").write_text('{"k":"FAKE"}', encoding="utf-8")

    def test_bounded_inventory_is_anonymous_and_content_free(self):
        result = probe.describe(self.root, synthetic=True)
        self.assertEqual(result["result"], "PASS_METADATA")
        self.assertEqual(result["codex_generation_files"]["codex-generation.db"]["status"], "present")
        self.assertEqual(result["codex_generation_files"]["codex-generation.db-wal"]["status"], "missing")
        self.assertEqual(result["codex_home"]["files"], 2)
        self.assertEqual(result["anonymous_unclassified_root"]["total"], 3)
        b = result["anonymous_unclassified_root"]["buckets"]
        self.assertEqual(b["sqlite_candidates"], 1)
        self.assertEqual(b["secret_extension_candidates"], 1)
        self.assertEqual(b["structured_config_candidates"], 1)
        serialized = json.dumps(result)
        for secret in ("private-token", "private-credential", "not-reviewed",
                       "unreviewed-config", "NEVER_PUBLISH", "FAKE"):
            self.assertNotIn(secret, serialized)
        self.assertFalse(result["contents_read"])
        self.assertFalse(result["source_writes"])
        self.assertFalse(result["restoration_tested"])

    def test_known_database_symlink_is_not_followed(self):
        db = self.root / "codex-generation.db"
        db.unlink()
        try:
            db.symlink_to(self.root / "secret-outside")
        except OSError:
            self.skipTest("Windows symbolic link privilege not available")
        result = probe.describe(self.root, synthetic=True)
        self.assertEqual(result["codex_generation_files"]["codex-generation.db"]["status"], "wrong_type")
        self.assertEqual(result["codex_generation_files"]["codex-generation.db"]["type"], "symlink")

    def test_codex_home_symlink_is_not_scanned(self):
        home = self.root / "codex-home"
        for child in (home / "private-token.txt", home / "deep" / "session-history.json"):
            child.unlink()
        (home / "deep").rmdir()
        home.rmdir()
        try:
            home.symlink_to(self.root / "missing-home", target_is_directory=True)
        except OSError:
            self.skipTest("Windows symbolic link privilege not available")
        result = probe.describe(self.root, synthetic=True)
        self.assertEqual(result["codex_home_status"], "wrong_type")
        self.assertIsNone(result["codex_home"])

    def test_depth_limit_marks_partial_not_complete(self):
        with mock.patch.object(probe, "DEPTH_CAP", 0):
            outcome = probe.describe(self.root, synthetic=True)
        self.assertTrue(outcome["codex_home"]["truncated"])
        self.assertEqual(outcome["codex_home"]["stop_reason"], "depth_limit")
        self.assertEqual(outcome["result"], "PARTIAL_METADATA")

    def test_cli_requires_synthetic_gate_and_prints_no_names(self):
        env = os.environ.pop("GUITING_STAGE47_SYNTHETIC_ONLY", None)
        self.addCleanup(lambda: (os.environ.pop("GUITING_STAGE47_SYNTHETIC_ONLY", None),
                                 os.environ.__setitem__("GUITING_STAGE47_SYNTHETIC_ONLY", env))
                        if env is not None else os.environ.pop("GUITING_STAGE47_SYNTHETIC_ONLY", None))
        argv = ["stage47", "--perform-readonly", "--synthetic-root", str(self.root)]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(probe.main(), 2)
        self.assertIn("FAIL_METADATA", out.getvalue())
        os.environ["GUITING_STAGE47_SYNTHETIC_ONLY"] = "true"
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(probe.main(), 0)
        text = out.getvalue()
        self.assertIn("PASS_METADATA", text)
        self.assertNotIn("private-token", text)


if __name__ == "__main__":
    unittest.main()
