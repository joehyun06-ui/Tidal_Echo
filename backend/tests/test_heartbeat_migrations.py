from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import channel_store


class HeartbeatMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "heartbeat-migration.sqlite3")

    def test_v5_schema_is_repeatable_and_strictly_validated(self):
        channel_store.run_migrations(self.path)
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            versions = [row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            channel_store.validate_heartbeat_schema(conn)
        self.assertEqual(versions, [1, 2, 3, 4, 5])
        self.assertTrue({
            "heartbeat_state", "heartbeat_runs", "journal_entries", "timeline_events",
        }.issubset(tables))

    def test_v4_upgrade_preserves_telegram_and_kelivo_data(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:4])
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO channel_accounts
                   (channel,external_account_id,status,created_at,updated_at)
                   VALUES('telegram','existing-bot','active',?,?)""", (stamp, stamp),
            )
            conn.execute(
                """INSERT INTO kelivo_clients
                   (client_id,api_session,enabled,mapping_revision,created_at,updated_at)
                   VALUES('existing-client','existing-session',1,1,?,?)""", (stamp, stamp),
            )
            before = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("channel_accounts", "kelivo_clients", "kelivo_requests")
            }
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            after = {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in before
            }
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute(
                "SELECT external_account_id FROM channel_accounts"
            ).fetchone()[0], "existing-bot")
            self.assertEqual(conn.execute(
                "SELECT api_session FROM kelivo_clients"
            ).fetchone()[0], "existing-session")
        self.assertEqual(before, after)

    def test_actual_v5_failure_rolls_back_all_new_schema_and_marker(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:4])
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO channel_accounts
                   (channel,external_account_id,status,created_at,updated_at)
                   VALUES('telegram','rollback-bot','active',?,?)""", (stamp, stamp),
            )

        def broken_v5(conn):
            channel_store._migration_005(conn)
            raise RuntimeError("injected v5 failure")

        migrations = (*channel_store.MIGRATIONS[:4], (5, "dylan_heartbeat_foundation", broken_v5))
        with self.assertRaisesRegex(RuntimeError, "injected v5"):
            channel_store.run_migrations(self.path, migrations)
        with channel_store.connect(self.path) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            marker = conn.execute("SELECT status FROM schema_migrations WHERE version=5").fetchone()
            bot = conn.execute("SELECT external_account_id FROM channel_accounts").fetchone()[0]
            channel_store.validate_kelivo_schema(conn)
        self.assertFalse({
            "heartbeat_state", "heartbeat_runs", "journal_entries", "timeline_events",
        } & tables)
        self.assertIsNone(marker)
        self.assertEqual(bot, "rollback-bot")

    def test_validator_rejects_column_check_index_and_foreign_key_corruption(self):
        cases = (
            ("column", lambda ddl: ddl.replace("last_tick_at TEXT", "last_tick_epoch TEXT"), None, None),
            ("check", lambda ddl: ddl.replace("consecutive_failures >= 0", "consecutive_failures >= -1"), None, None),
            ("index", None, lambda ddl: ddl.replace("scheduled_at,outcome,id", "outcome,scheduled_at,id"), None),
            ("foreign-key", None, None, lambda ddl: ddl.replace(
                "REFERENCES heartbeat_runs(id)", "REFERENCES heartbeat_runs(run_id)"
            )),
        )
        for label, state_transform, index_transform, journal_transform in cases:
            with self.subTest(label=label):
                target = str(Path(self.temp.name) / f"corrupt-{label}.sqlite3")
                with channel_store.connect(target) as conn:
                    for name, ddl in channel_store.HEARTBEAT_TABLE_DDL.items():
                        if name == "heartbeat_state" and state_transform:
                            ddl = state_transform(ddl)
                        if name == "journal_entries" and journal_transform:
                            ddl = journal_transform(ddl)
                        conn.execute(ddl)
                    for name, ddl in channel_store.HEARTBEAT_INDEX_DDL.items():
                        if name == "idx_heartbeat_runs_schedule" and index_transform:
                            ddl = index_transform(ddl)
                        conn.execute(ddl)
                with channel_store.connect(target) as conn, self.assertRaises(sqlite3.DatabaseError):
                    channel_store.validate_heartbeat_schema(conn)


if __name__ == "__main__":
    unittest.main()
