"""Stage45 metadata-only inventory: synthetic files, no production or network."""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import render_stage45_persistent_inventory_readonly as inventory


class Stage45SyntheticInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stage45-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "relay.db").write_bytes(b"synthetic" * 5)
        (self.root / "brain_target").write_text("loop\n", encoding="utf-8")
        (self.root / "api_loop.config.json").write_text("{}", encoding="utf-8")
        (self.root / "uploads").mkdir()
        (self.root / "uploads" / "private-example.png").write_bytes(b"synthetic image")
        (self.root / "codex-home").mkdir()
        (self.root / "not-in-manifest.file").write_bytes(b"not a secret")

    def test_known_paths_metadata_only_and_unknown_names_not_disclosed(self):
        result = inventory.inventory(self.root)
        self.assertEqual(result["items"]["relay_db"]["status"], "present")
        self.assertEqual(result["items"]["relay_wal"]["status"], "missing")
        self.assertEqual(result["items"]["uploads"]["inventory"]["files"], 1)
        self.assertEqual(result["unclassified_root_entries"]["count"], 1)
        self.assertNotIn("private-example", json.dumps(result))
        self.assertNotIn("not-in-manifest", json.dumps(result))
        self.assertFalse(result["file_contents_read"])
        self.assertFalse(result["actual_restore_proven"])

    def test_broken_known_symlink_refused_without_following_target(self):
        path = self.root / "uploads" / "private-example.png"
        path.unlink()
        try:
            path.symlink_to(self.root / "outside-private")
        except OSError:
            self.skipTest("OS denies symlink creation")
        self.assertEqual(inventory.inventory(self.root)["items"]["uploads"]["inventory"]["symlinks"], 1)
        db = self.root / "relay.db"
        db.unlink()
        try:
            db.symlink_to(self.root / "outside-remote")
        except OSError:
            self.skipTest("OS denies symlink creation")
        result = inventory.inventory(self.root)
        self.assertEqual(result["items"]["relay_db"]["status"], "type_mismatch")
        self.assertEqual(result["items"]["relay_db"]["type"], "symlink")

    def test_synthetic_mode_requires_explicit_gate(self):
        old = os.environ.pop("GUITING_STAGE45_SYNTHETIC_ONLY", None)
        self.addCleanup(lambda: os.environ.__setitem__("GUITING_STAGE45_SYNTHETIC_ONLY", old)
                        if old is not None else os.environ.pop("GUITING_STAGE45_SYNTHETIC_ONLY", None))
        with mock.patch("sys.argv", ["program", "--perform-readonly", "--synthetic-root", str(self.root)]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(inventory.main(), 2)
        self.assertIn("INVENTORY_UNAVAILABLE", out.getvalue())
        os.environ["GUITING_STAGE45_SYNTHETIC_ONLY"] = "true"
        with mock.patch("sys.argv", ["program", "--perform-readonly", "--synthetic-root", str(self.root)]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(inventory.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["root"], "SYNTHETIC")


if __name__ == "__main__":
    unittest.main()
