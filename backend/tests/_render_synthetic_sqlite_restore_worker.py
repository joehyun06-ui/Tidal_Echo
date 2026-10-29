"""Stage41: synthetic SQLite recovery exercises; no production path or operator CLI.

Every file and row is created in TemporaryDirectory. The only imported runtime
code is the current repository's SQLite schema/migration validation.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from backend import channel_store
from backend.tests._support import NoNetworkMixin


STAMP = "2026-10-29T00:00:00+00:00"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SyntheticSQLiteRestoreDrill(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="stage41-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(strict=True)
        self.source = self.root / "synthetic-source.sqlite3"

        # A fixture only. The core includes a real relay schema with v1-v11
        # channel/memory migrations, but no real messages or provider credentials.
        with closing(sqlite3.connect(self.source)) as conn:
            for ddl in channel_store.RELAY_TABLE_DDL.values():
                conn.execute(ddl)
            conn.commit()
        channel_store.run_migrations(str(self.source))
        self.assertEqual(self._verify(self.source), 0)

    def _safe(self, path: Path) -> Path:
        path = Path(path)
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent.resolve(strict=True), self.root)
        self.assertFalse(path.is_symlink())
        return path

    def _writer(self):
        conn = sqlite3.connect(self._safe(self.source))
        self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        return conn

    def _add_message(self, conn: sqlite3.Connection, marker: str) -> None:
        conn.execute(
            "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
            (STAMP, "in", "user", marker, "{}"),
        )
        conn.commit()

    def _backup(self, source: sqlite3.Connection, dest: Path) -> None:
        dest = self._safe(dest)
        self.assertFalse(dest.exists())
        with closing(sqlite3.connect(dest)) as output:
            source.backup(output, pages=64, sleep=0.01)
        self.assertTrue(dest.is_file())
        self.assertGreater(dest.stat().st_size, 0)

    def _verify(self, path: Path) -> int:
        path = self._safe(path)
        with closing(channel_store.connect_read_only(path, timeout_seconds=5)) as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            if len(rows) != 1 or tuple(rows[0]) != ("ok",):
                raise sqlite3.DatabaseError("synthetic_integrity_failed")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.DatabaseError("synthetic_foreign_key_failed")
            channel_store.validate_core_schema_v1_v6(conn, require_relay_tables=True)
            # Validate the v7-v11 migration-owned objects without requiring the\n            # operator's extra strict main-schema object inventory.
            channel_store.validate_memory_index_outbox_schema_v1_v11(conn)
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def test_consistent_online_backup_restores_wal_commits_and_preserves_point_in_time(self):
        with closing(self._writer()) as writer:
            self._add_message(writer, "synthetic_before_backup")
            self.assertTrue(Path(str(self.source) + "-wal").is_file())
            backup, restored = self.root / "consistent.sqlite3", self.root / "restored.sqlite3"
            self._backup(writer, backup)
            self.assertEqual(self._verify(backup), 1)
            self._add_message(writer, "synthetic_after_backup")
            self.assertEqual(self._verify(self.source), 2)
            shutil.copyfile(self._safe(backup), self._safe(restored))
            self.assertEqual(_sha256(backup), _sha256(restored))
            self.assertEqual(self._verify(restored), 1)
            # Recovery never overwrites the source fixture.
            self.assertEqual(self._verify(self.source), 2)

    def test_main_db_only_copy_misses_uncheckpointed_wal(self):
        with closing(self._writer()) as writer:
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._add_message(writer, "synthetic_only_in_wal")
            wal = Path(str(self.source) + "-wal")
            self.assertTrue(wal.is_file())
            self.assertGreater(wal.stat().st_size, 32)
            unsafe = self.root / "unsafe-main-file-only.sqlite3"
            safe = self.root / "safe-online-backup.sqlite3"
            shutil.copyfile(self._safe(self.source), self._safe(unsafe))
            self._backup(writer, safe)
            self.assertEqual(self._verify(safe), 1)
            self.assertNotEqual(self._verify(unsafe), 1)

    def test_restore_rejects_deleted_core_schema_table(self):
        with closing(sqlite3.connect(self.source)) as writer:
            backup = self.root / "valid.sqlite3"
            self._backup(writer, backup)
        invalid = self.root / "missing-schema.sqlite3"
        shutil.copyfile(backup, invalid)
        with closing(sqlite3.connect(invalid)) as connection:
            connection.execute("DROP TABLE delivery_parts")
            connection.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            self._verify(invalid)

    def test_restore_rejects_orphan_foreign_key_even_with_valid_table_shapes(self):
        with closing(sqlite3.connect(self.source)) as writer:
            backup = self.root / "valid.sqlite3"
            self._backup(writer, backup)
        invalid = self.root / "orphan.sqlite3"
        shutil.copyfile(backup, invalid)
        with closing(sqlite3.connect(invalid)) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "INSERT INTO generation_jobs"
                "(inbound_message_id,canonical_message_id,channel,external_account_id,"
                "external_conversation_id,api_session,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (999999, 1, "telegram", "synthetic", "synthetic", "synthetic",
                 "queued", STAMP, STAMP),
            )
            conn.commit()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "synthetic_foreign_key_failed"):
            self._verify(invalid)

    def test_restore_rejects_corrupted_database_header(self):
        with closing(sqlite3.connect(self.source)) as writer:
            backup = self.root / "valid.sqlite3"
            self._backup(writer, backup)
        invalid = self.root / "corrupted.sqlite3"
        shutil.copyfile(backup, invalid)
        with invalid.open("r+b") as handle:
            handle.write(b"not-a-sqlite-db!")
        with self.assertRaises(sqlite3.DatabaseError):
            self._verify(invalid)

    def test_recovered_copy_is_read_only_to_validator(self):
        with closing(sqlite3.connect(self.source)) as writer:
            backup = self.root / "valid.sqlite3"
            self._backup(writer, backup)
        with closing(channel_store.connect_read_only(backup, timeout_seconds=5)) as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO messages(ts,direction,kind,text,meta) VALUES(?,?,?,?,?)",
                    (STAMP, "in", "user", "must-not-write", "{}"),
                )
        self.assertEqual(self._verify(backup), 0)


if __name__ == "__main__":
    unittest.main()
