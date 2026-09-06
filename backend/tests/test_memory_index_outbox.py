from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import channel_store


class MemoryIndexOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "memory-index-outbox.sqlite3")
        self._prepare(self.path)

    @staticmethod
    def _prepare(path: str, migrations=None) -> None:
        with channel_store.connect(path) as conn:
            conn.execute(
                """CREATE TABLE messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                    text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')"""
            )
        channel_store.run_migrations(
            path,
            channel_store.MIGRATIONS if migrations is None else migrations,
        )

    def test_fresh_database_has_exact_v11_outbox_contract(self):
        with channel_store.connect(self.path) as conn:
            markers = [
                tuple(row)
                for row in conn.execute(
                    """SELECT version,name,status FROM schema_migrations
                       ORDER BY version"""
                )
            ]
            columns = tuple(
                (
                    row["name"], str(row["type"]).upper(),
                    int(row["notnull"]), row["dflt_value"], int(row["pk"]),
                )
                for row in conn.execute(
                    "PRAGMA table_xinfo(memory_index_outbox)"
                )
            )
            indexes = {
                row["name"]: (
                    bool(row["unique"]), row["origin"], bool(row["partial"]),
                    channel_store._index_columns(conn, row["name"]),
                )
                for row in conn.execute(
                    "PRAGMA index_list(memory_index_outbox)"
                )
            }
            triggers = {
                row["name"]
                for row in conn.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='trigger'
                         AND tbl_name='memory_index_outbox'"""
                )
            }
            foreign_keys = conn.execute(
                "PRAGMA foreign_key_list(memory_index_outbox)"
            ).fetchall()
            channel_store.validate_memory_index_outbox_schema_v1_v11(conn)

        self.assertEqual(len(markers), 11)
        self.assertEqual(
            markers[-1],
            (11, "memory_index_dirty_outbox_foundation", "applied"),
        )
        self.assertEqual(
            columns,
            (
                ("id", "INTEGER", 0, None, 1),
                ("event_kind", "TEXT", 1, None, 0),
                ("created_at", "TEXT", 1, None, 0),
                ("completed_at", "TEXT", 0, None, 0),
            ),
        )
        self.assertEqual(
            indexes,
            {
                "idx_memory_index_outbox_pending": (
                    False, "c", True, ("id",),
                ),
            },
        )
        self.assertEqual(
            triggers,
            set(channel_store.MEMORY_INDEX_OUTBOX_TRIGGER_DDL),
        )
        self.assertEqual(foreign_keys, [])

    def test_enqueue_requires_existing_transaction_and_stores_no_identity(self):
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            with self.assertRaisesRegex(
                sqlite3.DatabaseError,
                "transaction required",
            ):
                channel_store.enqueue_memory_index_dirty(
                    conn,
                    created_at=stamp,
                )
            conn.execute("BEGIN IMMEDIATE")
            event_id = channel_store.enqueue_memory_index_dirty(
                conn,
                created_at=stamp,
            )
            row = conn.execute(
                "SELECT * FROM memory_index_outbox WHERE id=?",
                (event_id,),
            ).fetchone()
            self.assertEqual(
                tuple(row),
                (
                    event_id,
                    channel_store.MEMORY_INDEX_DIRTY_EVENT_KIND,
                    stamp,
                    None,
                ),
            )
            self.assertEqual(
                set(row.keys()),
                {"id", "event_kind", "created_at", "completed_at"},
            )
            for forbidden in (
                "memory_key", "content", "query", "vector", "model",
                "scope", "kind", "fingerprint",
            ):
                self.assertNotIn(forbidden, row.keys())
            conn.execute("ROLLBACK")
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM memory_index_outbox"
                ).fetchone()[0],
                0,
            )

    def test_event_checks_completion_monotonicity_and_safe_pruning(self):
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            event_id = channel_store.enqueue_memory_index_dirty(
                conn,
                created_at=stamp,
            )
            conn.execute("COMMIT")

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "memory_index_outbox_pending_delete",
            ):
                conn.execute(
                    "DELETE FROM memory_index_outbox WHERE id=?",
                    (event_id,),
                )
            conn.execute(
                "UPDATE memory_index_outbox SET completed_at=? WHERE id=?",
                (stamp, event_id),
            )
            for value in (None, stamp):
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "memory_index_outbox_completion_invalid",
                ):
                    conn.execute(
                        """UPDATE memory_index_outbox SET completed_at=?
                           WHERE id=?""",
                        (value, event_id),
                    )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "memory_index_outbox_identity_immutable",
            ):
                conn.execute(
                    "UPDATE memory_index_outbox SET created_at=? WHERE id=?",
                    (channel_store.now_iso(), event_id),
                )
            conn.execute(
                "DELETE FROM memory_index_outbox WHERE id=?",
                (event_id,),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM memory_index_outbox"
                ).fetchone()[0],
                0,
            )

    def test_invalid_event_kind_and_timestamps_fail_closed(self):
        stamp = channel_store.now_iso()
        with channel_store.connect(self.path) as conn:
            for values in (
                ("other", stamp, None),
                (channel_store.MEMORY_INDEX_DIRTY_EVENT_KIND, "bad", None),
                (
                    channel_store.MEMORY_INDEX_DIRTY_EVENT_KIND,
                    stamp,
                    "2000-01-01T00:00:00+00:00",
                ),
            ):
                with self.subTest(values=values), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    conn.execute(
                        """INSERT INTO memory_index_outbox
                           (event_kind,created_at,completed_at)
                           VALUES(?,?,?)""",
                        values,
                    )

    def test_failed_v11_rolls_back_every_owned_object_and_marker(self):
        path = str(Path(self.temp.name) / "failed-v11.sqlite3")
        self._prepare(path, channel_store.MIGRATIONS[:10])

        def broken(conn):
            channel_store._migration_011(conn)
            raise RuntimeError("injected-v11")

        migrations = (
            *channel_store.MIGRATIONS[:10],
            (11, "memory_index_dirty_outbox_foundation", broken),
        )
        with self.assertRaisesRegex(RuntimeError, "^injected-v11$"):
            channel_store.run_migrations(path, migrations)
        with channel_store.connect(path) as conn:
            objects = conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE name LIKE 'memory_index_outbox%'
                      OR name LIKE 'idx_memory_index_outbox%'"""
            ).fetchall()
            marker = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=11"
            ).fetchone()
            self.assertEqual(objects, [])
            self.assertIsNone(marker)
            channel_store.validate_memory_candidate_decision_schema_v1_v10(
                conn
            )

    def test_v11_is_additive_and_validator_rejects_owned_drift(self):
        path = str(Path(self.temp.name) / "additive-v11.sqlite3")
        self._prepare(path, channel_store.MIGRATIONS[:10])
        with channel_store.connect(path) as conn:
            before = {
                (row["type"], row["name"]): row["sql"]
                for row in conn.execute(
                    """SELECT type,name,sql FROM sqlite_master
                       WHERE name NOT LIKE 'sqlite_autoindex_%'
                         AND name NOT LIKE 'sqlite_%'"""
                )
            }
        channel_store.run_migrations(path)
        with channel_store.connect(path) as conn:
            after = {
                key: conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                    key,
                ).fetchone()[0]
                for key in before
            }
            channel_store.validate_memory_index_outbox_schema_v1_v11(conn)
        self.assertEqual(after, before)

        corruptions = (
            (
                "marker",
                "UPDATE schema_migrations SET name='wrong' WHERE version=11",
            ),
            ("index", "DROP INDEX idx_memory_index_outbox_pending"),
            (
                "index-predicate",
                """DROP INDEX idx_memory_index_outbox_pending;
                   CREATE INDEX idx_memory_index_outbox_pending
                   ON memory_index_outbox(id)
                   WHERE completed_at IS NOT NULL""",
            ),
            (
                "trigger",
                "DROP TRIGGER memory_index_outbox_completion_monotonic",
            ),
            (
                "owned-object",
                "CREATE INDEX idx_memory_index_outbox_extra "
                "ON memory_index_outbox(created_at)",
            ),
            (
                "table-fingerprint",
                """PRAGMA writable_schema=ON;
                   UPDATE sqlite_master
                      SET sql=replace(
                          sql,
                          'active_atomic_snapshot_dirty',
                          'active_atomic_snapshot_changed'
                      )
                    WHERE type='table' AND name='memory_index_outbox';
                   PRAGMA writable_schema=OFF""",
            ),
        )
        for name, script in corruptions:
            with self.subTest(name=name):
                corrupt = str(Path(self.temp.name) / f"corrupt-{name}.sqlite3")
                self._prepare(corrupt)
                with channel_store.connect(corrupt) as conn:
                    conn.executescript(script)
                with channel_store.connect(corrupt) as conn, self.assertRaises(
                    sqlite3.DatabaseError
                ):
                    channel_store.validate_memory_index_outbox_schema_v1_v11(
                        conn
                    )


if __name__ == "__main__":
    unittest.main()
