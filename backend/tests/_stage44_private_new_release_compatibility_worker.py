"""Stage44 synthetic-only tests. NEVER load real operator backups in CI."""
from __future__ import annotations
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from backend import channel_store
from scripts import stage44_private_new_release_compatibility as probe


class Stage44SyntheticRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stage44-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.localapp = self.root / "local-app"
        self.localapp.mkdir()
        old_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old_env)))
        os.environ["LOCALAPPDATA"] = str(self.localapp)
        owner_dir = self.localapp / "GuitingPrivateRecovery"
        owner_dir.mkdir()
        self.backup = owner_dir / "relay-backup.sqlite3"
        with closing(sqlite3.connect(self.backup)) as conn:
            for sql in channel_store.RELAY_TABLE_DDL.values():
                conn.execute(sql)
            conn.commit()
        channel_store.run_migrations(str(self.backup))
        with closing(sqlite3.connect(self.backup)) as conn:
            conn.execute("INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                         ("synthetic", "in", "user", "fake-old-backup-row", "{}"))
            conn.commit()
        self.digest = probe.sha256(self.backup)
        self.repo = Path(__file__).resolve().parents[2]

    @staticmethod
    def fake_git(cmd, **kwargs):
        output = (probe.EXPECTED_HEAD + "\n") if "rev-parse" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    def test_synthetic_restored_copy_reaches_real_asgi_health_without_network(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=self.fake_git):
            outcome = probe.guard_run(self.repo, self.backup, self.digest)
        self.assertEqual(outcome["result"], "PASS")
        self.assertEqual(outcome["core_startup"], "PASS")
        self.assertEqual(outcome["restored_database_bound_to_app"], "true")
        self.assertEqual(outcome["existing_messages_preserved"], "true")
        self.assertEqual(outcome["local_asgi_healthz"], "PASS")
        self.assertEqual(probe.sha256(self.backup), self.digest)
        self.assertEqual(list(self.localapp.glob("guiting-stage44-*")), [])

    def test_wrong_expected_digest_rejected(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=self.fake_git):
            with self.assertRaisesRegex(ValueError, "BACKUP_HASH_MISMATCH"):
                probe.guard_run(self.repo, self.backup, "0" * 64)

    def test_symlink_source_rejected(self):
        fake = self.root / "fake.sqlite3"
        fake.write_text("not a database", encoding="utf-8")
        self.backup.unlink()
        try:
            self.backup.symlink_to(fake)
        except OSError:
            self.skipTest("Windows symlink permission unavailable")
        with mock.patch.object(probe.subprocess, "run", side_effect=self.fake_git):
            with self.assertRaisesRegex(ValueError, "BACKUP_INVALID"):
                probe.guard_run(self.repo, self.backup, self.digest)


if __name__ == "__main__":
    unittest.main()
