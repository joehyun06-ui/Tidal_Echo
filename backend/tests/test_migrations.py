import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from backend import channel_store


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "migration.sqlite3")
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
            conn.execute("INSERT INTO messages(text) VALUES('preserve me')")
            conn.execute("CREATE TABLE push_subscriptions(endpoint TEXT PRIMARY KEY)")
            conn.commit()

    def test_migration_preserves_legacy_data_and_is_repeatable(self):
        channel_store.run_migrations(self.db_path)
        channel_store.run_migrations(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT text FROM messages").fetchone()[0], "preserve me")
            self.assertEqual(conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0],
                             len(channel_store.MIGRATIONS))
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"channel_accounts", "channel_conversations", "inbound_events",
                         "external_messages", "generation_jobs", "delivery_attempts",
                         "channel_audit_events", "telegram_completions", "delivery_parts"}.issubset(tables))

    def test_failed_migration_rolls_back(self):
        channel_store.run_migrations(self.db_path)

        def broken(conn):
            conn.execute("CREATE TABLE should_rollback(id INTEGER)")
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            channel_store.run_migrations(self.db_path, [(5, "broken", broken)])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='should_rollback'"
            ).fetchone())
            self.assertIsNone(conn.execute("SELECT version FROM schema_migrations WHERE version=5").fetchone())

    def test_actual_v2_failure_rolls_back_every_schema_change(self):
        channel_store.run_migrations(self.db_path, [channel_store.MIGRATIONS[0]])
        original_connect = channel_store.connect

        def guarded_connect(path):
            conn = original_connect(path)
            def authorizer(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_CREATE_TABLE and arg1 == "delivery_parts":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorizer)
            return conn

        with mock.patch.object(channel_store, "connect", new=guarded_connect):
            with self.assertRaises(sqlite3.DatabaseError):
                channel_store.run_migrations(self.db_path, [channel_store.MIGRATIONS[1]])
        with closing(sqlite3.connect(self.db_path)) as conn:
            versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            job_columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_jobs)")}
            self.assertEqual(conn.execute("SELECT text FROM messages").fetchone()[0], "preserve me")
        self.assertEqual(versions, {1})
        self.assertNotIn("telegram_completions", tables)
        self.assertNotIn("delivery_parts", tables)
        self.assertNotIn("dispatch_started_at", job_columns)

        channel_store.run_migrations(self.db_path, [channel_store.MIGRATIONS[1]])
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM schema_migrations WHERE version=2").fetchone()[0], "applied")


if __name__ == "__main__":
    unittest.main()
