from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import channel_store


class MemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "memory.sqlite3")
        with channel_store.connect(self.path) as conn:
            conn.execute("""CREATE TABLE messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")

    def test_empty_database_upgrades_to_v7_and_is_repeatable(self):
        channel_store.run_migrations(self.path)
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            versions = [
                row[0] for row in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            channel_store.validate_memory_schema(conn)
        self.assertEqual(versions, list(range(1, 8)))
        self.assertTrue(
            {
                "memory_items",
                "memory_fingerprint_profile",
                "memory_evidence_events",
                "memory_sources",
                "memory_suppressions",
            }.issubset(tables)
        )

    def test_every_synthetic_prior_version_upgrades_to_v7(self):
        for version in range(1, 7):
            with self.subTest(version=version):
                path = str(Path(self.temp.name) / f"v{version}.sqlite3")
                with channel_store.connect(path) as conn:
                    conn.execute("""CREATE TABLE messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                        text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
                channel_store.run_migrations(path, channel_store.MIGRATIONS[:version])
                with channel_store.connect(path) as conn:
                    stamp = channel_store.now_iso()
                    conn.execute(
                        """INSERT INTO channel_accounts
                           (channel,external_account_id,status,created_at,updated_at)
                           VALUES('telegram',?,'active',?,?)""",
                        (f"synthetic-v{version}", stamp, stamp),
                    )
                channel_store.run_migrations(path)
                with channel_store.connect(path) as conn:
                    marker = conn.execute(
                        "SELECT status FROM schema_migrations WHERE version=7"
                    ).fetchone()
                    preserved = conn.execute(
                        "SELECT count(*) FROM channel_accounts"
                    ).fetchone()[0]
                    channel_store.validate_memory_schema(conn)
                self.assertEqual(marker[0], "applied")
                self.assertEqual(preserved, 1)

    def test_concurrent_optional_v7_migration_applies_exactly_once(self):
        channel_store.run_migrations(self.path, channel_store.CORE_MIGRATIONS)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(
                lambda _index: channel_store.run_migrations(self.path),
                range(16),
            ))
        with channel_store.connect(self.path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM schema_migrations WHERE version=7"
                ).fetchone()[0],
                1,
            )
            channel_store.validate_memory_schema(conn)

    def test_v6_relational_rows_are_preserved(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:6])
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO channel_accounts
                   (channel,external_account_id,status,created_at,updated_at)
                   VALUES('telegram','synthetic-account','active',?,?)""",
                (stamp, stamp),
            )
            before = {
                row["name"]: row["sql"] for row in conn.execute(
                    """SELECT name,sql FROM sqlite_master
                       WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"""
                )
            }
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM channel_accounts").fetchone()[0], 1
            )
            after_old = {
                row["name"]: row["sql"] for row in conn.execute(
                    """SELECT name,sql FROM sqlite_master
                       WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'
                         AND name NOT LIKE 'memory_%' AND name NOT LIKE 'idx_memory_%'"""
                )
            }
        self.assertEqual(after_old, before)

    def test_failed_v7_rolls_back_tables_indexes_and_marker(self):
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:6])

        def broken(conn):
            channel_store._migration_007(conn)
            raise RuntimeError("injected")

        migrations = (*channel_store.MIGRATIONS[:6], (7, "explicit_memory_core_foundation", broken))
        with self.assertRaisesRegex(RuntimeError, "injected"):
            channel_store.run_migrations(self.path, migrations)
        with channel_store.connect(self.path) as conn:
            objects = conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE name LIKE 'memory_%' OR name LIKE 'idx_memory_%'"""
            ).fetchall()
            marker = conn.execute(
                "SELECT status FROM schema_migrations WHERE version=7"
            ).fetchone()
        self.assertEqual(objects, [])
        self.assertIsNone(marker)

    def test_validator_rejects_structural_corruption(self):
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            conn.execute("DROP INDEX idx_memory_items_live_fingerprint")
        with channel_store.connect(self.path) as conn, self.assertRaisesRegex(
            sqlite3.DatabaseError, "memory index"
        ):
            channel_store.validate_memory_schema(conn)
        with self.assertRaises(sqlite3.DatabaseError):
            channel_store.run_migrations(self.path)

    def test_validator_rejects_missing_or_modified_immutability_trigger(self):
        for index, statement in enumerate((
            "DROP TRIGGER memory_evidence_events_immutable_update",
            """DROP TRIGGER memory_evidence_events_immutable_delete;
               CREATE TRIGGER memory_evidence_events_immutable_delete
               BEFORE DELETE ON memory_evidence_events
               BEGIN SELECT RAISE(ABORT,'wrong_category'); END""",
        )):
            with self.subTest(index=index):
                path = str(Path(self.temp.name) / f"trigger-{index}.sqlite3")
                with channel_store.connect(path) as conn:
                    conn.execute("""CREATE TABLE messages(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                        text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')""")
                channel_store.run_migrations(path)
                with channel_store.connect(path) as conn:
                    conn.executescript(statement)
                with channel_store.connect(path) as conn, self.assertRaisesRegex(
                    sqlite3.DatabaseError, "memory trigger",
                ):
                    channel_store.validate_memory_schema(conn)

    def test_checks_partial_unique_and_restrict_fks_are_enforced(self):
        channel_store.run_migrations(self.path)
        stamp = channel_store.now_iso()
        digest = b"a" * 32
        with channel_store.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO messages(ts,direction,kind,text,meta)
                   VALUES(?,'in','user','synthetic','{"channel":"web","source":"relay"}')""",
                (stamp,),
            )
            conn.execute(
                """INSERT INTO memory_items
                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                    normalized_fingerprint,fingerprint_version,status,explicitness,
                    confidence,sensitivity,first_observed_at,last_confirmed_at,
                    superseded_by_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1,'active','explicit',1.0,'normal',?,?,NULL,?,?)""",
                ("A" * 32, "project", "global_user", "", "synthetic", digest, stamp, stamp, stamp, stamp),
            )
            memory_id = conn.execute(
                "SELECT id FROM memory_items WHERE memory_key=?", ("A" * 32,)
            ).fetchone()[0]
            message_id = conn.execute("SELECT id FROM messages").fetchone()[0]
            evidence_cursor = conn.execute(
                """INSERT INTO memory_evidence_events
                   (canonical_message_id,action_id,action_type,action_binding_version,
                    evidence_type,reality_scope,subject_scope,
                    created_by_component,created_at)
                   VALUES(?,?,'remember_explicit_user',1,
                          'explicit_user_memory','real','user','memory_admin',?)""",
                (message_id, "A" * 32, stamp),
            )
            conn.execute(
                """INSERT INTO memory_sources
                   (memory_id,evidence_event_id,canonical_message_id,channel,source,
                    evidence_role,evidence_type,created_at)
                   VALUES(?,?,?,'web','relay','user','explicit_user_memory',?)""",
                (memory_id, int(evidence_cursor.lastrowid), message_id, stamp),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO memory_items
                       (memory_key,kind,scope_type,scope_ref,normalized_content,
                        normalized_fingerprint,fingerprint_version,status,explicitness,
                        confidence,sensitivity,first_observed_at,last_confirmed_at,
                        superseded_by_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,1,'candidate','explicit',1.0,'normal',?,?,NULL,?,?)""",
                    ("B" * 32, "project", "global_user", "", "duplicate", digest, stamp, stamp, stamp, stamp),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE memory_items SET status='forgotten' WHERE id=?", (memory_id,)
                )

    def test_v7_database_remains_compatible_with_v6_migration_path(self):
        channel_store.run_migrations(self.path)
        channel_store.run_migrations(self.path, channel_store.MIGRATIONS[:6])
        channel_store.run_migrations(self.path)
        with channel_store.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 7
            )
            channel_store.validate_kelivo_schema(conn)
            channel_store.validate_heartbeat_schema(conn)
            channel_store.validate_heartbeat_hardening_schema(conn)

    def test_v1_through_v6_migration_identity_is_unchanged(self):
        self.assertEqual(
            tuple((version, name) for version, name, _apply in channel_store.MIGRATIONS[:6]),
            (
                (1, "telegram_private_text_mvp"),
                (2, "telegram_reliability"),
                (3, "kelivo_nonstream_foundation"),
                (4, "kelivo_automatic_idempotency"),
                (5, "dylan_heartbeat_foundation"),
                (6, "dylan_heartbeat_hardening"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
