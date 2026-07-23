from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import channel_store
from backend.tests._support import NoNetworkMixin


CORE_TABLES = tuple(
    (
        *channel_store.CORE_V1_TABLE_DDL,
        *channel_store.CORE_V2_TABLE_DDL,
        *channel_store.KELIVO_TABLE_DDL,
        *channel_store.HEARTBEAT_TABLE_DDL,
        *channel_store.HEARTBEAT_HARDENING_TABLE_DDL,
    )
)

CORE_INDEXES = tuple(
    (
        *channel_store.CORE_V1_INDEX_DDL,
        *channel_store.CORE_V2_INDEX_DDL,
        *channel_store.KELIVO_INDEX_DDL,
        *channel_store.HEARTBEAT_INDEX_DDL,
        *channel_store.HEARTBEAT_HARDENING_INDEX_DDL,
    )
)


class CoreSchemaValidationTests(NoNetworkMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def path(self, name: str) -> str:
        return str(Path(self.temp.name) / f"{name}.sqlite3")

    def initialize(self, name: str, *, memory: bool = False) -> str:
        path = self.path(name)
        with channel_store.connect(path) as conn:
            for statement in channel_store.RELAY_TABLE_DDL.values():
                conn.execute(statement)
        channel_store.run_migrations(
            path,
            channel_store.MIGRATIONS
            if memory
            else channel_store.CORE_MIGRATIONS,
        )
        return path

    def validate(self, path: str) -> None:
        with channel_store.connect(path) as conn:
            channel_store.validate_core_schema_v1_v6(
                conn, require_relay_tables=True,
            )

    def test_empty_database_initializes_and_complete_v6_validates(self):
        path = self.initialize("complete-v6")
        self.validate(path)

    def test_every_v1_v6_core_table_missing_is_rejected(self):
        for index, table in enumerate(CORE_TABLES):
            with self.subTest(table=table):
                path = self.initialize(f"missing-table-{index}")
                with channel_store.connect(path) as conn:
                    conn.execute("PRAGMA foreign_keys=OFF")
                    conn.execute(f"DROP TABLE {table}")
                with self.assertRaises(sqlite3.DatabaseError):
                    self.validate(path)

    def test_every_explicit_core_index_missing_is_rejected(self):
        for index, name in enumerate(CORE_INDEXES):
            with self.subTest(index=name):
                path = self.initialize(f"missing-index-{index}")
                with channel_store.connect(path) as conn:
                    conn.execute(f"DROP INDEX {name}")
                with self.assertRaises(sqlite3.DatabaseError):
                    self.validate(path)

    def test_recorded_marker_does_not_mask_missing_objects(self):
        path = self.initialize("recorded-marker")
        with channel_store.connect(path) as conn:
            self.assertEqual(
                conn.execute(
                    """SELECT count(*) FROM schema_migrations
                       WHERE version BETWEEN 1 AND 6 AND status='applied'"""
                ).fetchone()[0],
                6,
            )
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE channel_rate_limits")
        with self.assertRaises(sqlite3.DatabaseError):
            channel_store.run_migrations(
                path, channel_store.CORE_MIGRATIONS,
            )

    def test_core_fk_check_and_unique_contract_corruption_is_rejected(self):
        corruptions = (
            (
                "fk",
                "delivery_parts",
                "FOREIGN KEY(delivery_id) REFERENCES delivery_attempts(id)",
                "CHECK(delivery_id>=0)",
            ),
            (
                "check",
                "kelivo_clients",
                "CHECK(enabled IN (0,1))",
                "CHECK(enabled IN (0,1,2))",
            ),
            (
                "unique",
                "channel_rate_limits",
                "UNIQUE(channel, external_account_id, external_user_id)",
                "CHECK(length(channel)>0)",
            ),
        )
        for name, table, original, replacement in corruptions:
            with self.subTest(name=name):
                path = self.initialize(f"corrupt-{name}")
                with channel_store.connect(path) as conn:
                    conn.execute("PRAGMA writable_schema=ON")
                    row = conn.execute(
                        """SELECT sql FROM sqlite_master
                           WHERE type='table' AND name=?""",
                        (table,),
                    ).fetchone()
                    self.assertIn(original, row["sql"])
                    conn.execute(
                        """UPDATE sqlite_master SET sql=replace(sql,?,?)
                           WHERE type='table' AND name=?""",
                        (original, replacement, table),
                    )
                    conn.execute("PRAGMA writable_schema=OFF")
                with self.assertRaises(sqlite3.DatabaseError):
                    self.validate(path)

    def test_complete_v7_and_corrupt_optional_v7_do_not_change_core_result(self):
        path = self.initialize("v7-disabled", memory=True)
        self.validate(path)
        with channel_store.connect(path) as conn:
            conn.execute("DROP INDEX idx_memory_items_live_fingerprint")
        self.validate(path)

    def test_complete_v7_remains_accepted_by_v6_migration_path(self):
        path = self.initialize("v7-v6-compatible", memory=True)
        channel_store.run_migrations(
            path, channel_store.CORE_MIGRATIONS,
        )
        self.validate(path)

    def test_concurrent_core_migration_validates_one_complete_schema(self):
        path = self.path("concurrent-core")

        def migrate(_index: int) -> None:
            channel_store.run_migrations(
                path, channel_store.CORE_MIGRATIONS,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(migrate, range(12)))
        with channel_store.connect(path) as conn:
            channel_store.validate_core_schema_v1_v6(conn)
            self.assertEqual(
                conn.execute(
                    """SELECT count(*) FROM schema_migrations
                       WHERE version BETWEEN 1 AND 6 AND status='applied'"""
                ).fetchone()[0],
                6,
            )


if __name__ == "__main__":
    unittest.main()
