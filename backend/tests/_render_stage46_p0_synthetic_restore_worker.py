"""Stage46: fake data only; GitHub CI must never receive operator uploads/keys."""
from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from scripts import render_stage46_p0_synthetic_restore as trial


class Stage46SyntheticP0Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stage46-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "uploads").mkdir()
        (self.root / "uploads" / "att-fake.png").write_bytes(b"FAKE-NOT-USER-DATA")
        (self.root / "api_loop.config.json").write_text(
            json.dumps({"main_chain": [{"url": "https://synthetic.invalid/v1",
                                       "model": "fake-test", "key": "FAKE-NOT-REAL-KEY"}]}),
            encoding="utf-8")
        with closing(sqlite3.connect(self.root / "synthetic-messages.sqlite3")) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, meta TEXT NOT NULL)")
            conn.execute("INSERT INTO messages(meta) VALUES (?)",
                         (json.dumps({"attachments": [{"url": "/uploads/att-fake.png"}]}),))
            conn.commit()

    def test_synthetic_copy_preserves_reference_and_app_config(self):
        cfg = self.root / "api_loop.config.json"
        original = trial.digest(cfg)
        status = trial.synthetic_restore(self.root)
        self.assertEqual(status["result"], "PASS")
        self.assertEqual(status["files_verified"], 1)
        self.assertEqual(trial.digest(cfg), original)
        self.assertNotIn("FAKE-NOT-REAL-KEY", json.dumps(status))
        self.assertNotIn("att-fake.png", json.dumps(status))

    def test_missing_referenced_attachment_rejected(self):
        (self.root / "uploads" / "att-fake.png").unlink()
        (self.root / "uploads" / "att-other.png").write_bytes(b"fake")
        with self.assertRaisesRegex(trial.DrillError, "fixture_reference_missing"):
            trial.synthetic_restore(self.root)

    def test_invalid_model_config_rejected(self):
        (self.root / "api_loop.config.json").write_text(
            json.dumps({"main_chain": [{"url": "https://synthetic.invalid/v1",
                                        "model": "fake", "key": ""}]}), encoding="utf-8")
        with self.assertRaises(trial.deployment_config.DeploymentConfigError):
            trial.synthetic_restore(self.root)

    def test_symlink_fixture_rejected(self):
        file = self.root / "uploads" / "att-fake.png"
        file.unlink()
        try:
            file.symlink_to(self.root / "outside")
        except OSError:
            self.skipTest("OS lacks symlink privilege")
        with self.assertRaisesRegex(trial.DrillError, "not_regular_file"):
            trial.synthetic_restore(self.root)

    def test_cli_requires_synthetic_gate(self):
        before = os.environ.pop("GUITING_STAGE46_SYNTHETIC_ONLY", None)
        self.addCleanup(lambda: os.environ.__setitem__("GUITING_STAGE46_SYNTHETIC_ONLY", before)
                        if before is not None else os.environ.pop("GUITING_STAGE46_SYNTHETIC_ONLY", None))
        args = ["test", "--perform-synthetic-restore", "--synthetic-root", str(self.root)]
        with mock.patch("sys.argv", args), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(trial.main(), 2)
        self.assertIn('"result":"FAIL"', output.getvalue())
        os.environ["GUITING_STAGE46_SYNTHETIC_ONLY"] = "true"
        with mock.patch("sys.argv", args), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(trial.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["scope"], "SYNTHETIC_ONLY")


if __name__ == "__main__":
    unittest.main()
