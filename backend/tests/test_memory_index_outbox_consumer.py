from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import channel_store
from backend import memory_index_outbox_consumer as consumer


CREATED = "2026-09-06T10:00:00+00:00"
COMPLETED = "2026-09-06T10:01:00+00:00"


class _FailAfterUpdateConnection:
    def __init__(self, connection):
        self.connection = connection
        self.updated = False

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def execute(self, statement, parameters=()):
        normalized = " ".join(statement.split())
        if normalized.startswith("UPDATE memory_index_outbox"):
            self.updated = True
        elif (
            self.updated
            and normalized.startswith("SELECT COUNT(*) FROM memory_index_outbox")
            and "completed_at IS NULL AND id<=?" in normalized
        ):
            raise sqlite3.OperationalError("private injected database detail")
        return self.connection.execute(statement, parameters)

    def close(self):
        self.connection.close()


class MemoryIndexOutboxConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = (Path(self.temp.name) / "relay.sqlite3").resolve()
        with channel_store.connect(str(self.path)) as conn:
            conn.execute(
                """CREATE TABLE messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                    text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')"""
            )
        channel_store.run_migrations(str(self.path), channel_store.MIGRATIONS)

    def enqueue(self, stamp: str = CREATED) -> int:
        with channel_store.connect(str(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            event_id = channel_store.enqueue_memory_index_dirty(
                conn,
                created_at=stamp,
            )
            conn.execute("COMMIT")
        return event_id

    def pending_rows(self):
        with channel_store.connect(str(self.path)) as conn:
            return tuple(
                tuple(row)
                for row in conn.execute(
                    """SELECT id,completed_at FROM memory_index_outbox
                       ORDER BY id"""
                )
            )

    def test_empty_read_is_none_and_never_opens_write_connection(self):
        with mock.patch.object(
            consumer.channel_store,
            "connect",
            side_effect=AssertionError("write connection forbidden"),
        ):
            self.assertIsNone(consumer.peek_pending_batch_v1(self.path))

    def test_fixed_watermark_coalesces_and_leaves_newer_event_pending(self):
        first = self.enqueue()
        second = self.enqueue()
        batch = consumer.peek_pending_batch_v1(self.path)
        self.assertEqual(batch.pending_count, 2)
        self.assertEqual(batch.high_watermark, second)

        third = self.enqueue("2026-09-06T10:00:30+00:00")
        receipt = consumer.complete_pending_batch_v1(
            self.path,
            batch,
            completed_at=COMPLETED,
        )
        self.assertEqual(receipt.completed_count, 2)
        self.assertEqual(
            self.pending_rows(),
            ((first, COMPLETED), (second, COMPLETED), (third, None)),
        )
        remaining = consumer.peek_pending_batch_v1(self.path)
        self.assertEqual(remaining.pending_count, 1)
        self.assertEqual(remaining.high_watermark, third)

    def test_repeated_completion_is_monotonic_and_idempotent(self):
        self.enqueue()
        batch = consumer.peek_pending_batch_v1(self.path)
        first = consumer.complete_pending_batch_v1(
            self.path,
            batch,
            completed_at=COMPLETED,
        )
        second = consumer.complete_pending_batch_v1(
            self.path,
            batch,
            completed_at=COMPLETED,
        )
        self.assertEqual(first.completed_count, 1)
        self.assertEqual(second.completed_count, 0)

    def test_batch_is_bound_to_the_database_that_produced_it(self):
        self.enqueue()
        batch = consumer.peek_pending_batch_v1(self.path)
        other = (Path(self.temp.name) / "other.sqlite3").resolve()
        with channel_store.connect(str(other)) as conn:
            conn.execute(
                """CREATE TABLE messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,direction TEXT NOT NULL,kind TEXT NOT NULL,
                    text TEXT NOT NULL,meta TEXT NOT NULL DEFAULT '{}')"""
            )
        channel_store.run_migrations(str(other), channel_store.MIGRATIONS)
        with self.assertRaises(consumer.MemoryIndexOutboxConsumerError) as raised:
            consumer.complete_pending_batch_v1(other, batch)
        self.assertEqual(
            raised.exception.category,
            "memory_index_outbox_batch_invalid",
        )
        self.assertEqual(self.pending_rows()[0][1], None)

        fabricated = dataclasses.replace(batch, _seal=object())
        with self.assertRaises(consumer.MemoryIndexOutboxConsumerError) as forged:
            consumer.complete_pending_batch_v1(self.path, fabricated)
        self.assertEqual(
            forged.exception.category,
            "memory_index_outbox_batch_invalid",
        )
        self.assertEqual(self.pending_rows()[0][1], None)

    def test_failure_after_update_rolls_back_the_entire_completion(self):
        self.enqueue()
        batch = consumer.peek_pending_batch_v1(self.path)
        real = channel_store.connect(str(self.path))
        proxy = _FailAfterUpdateConnection(real)
        with mock.patch.object(consumer.channel_store, "connect", return_value=proxy):
            with self.assertRaises(consumer.MemoryIndexOutboxConsumerError) as raised:
                consumer.complete_pending_batch_v1(
                    self.path,
                    batch,
                    completed_at=COMPLETED,
                )
        self.assertEqual(
            raised.exception.category,
            "memory_index_outbox_completion_failed",
        )
        self.assertEqual(self.pending_rows()[0][1], None)

    def test_schema_tamper_fails_closed_with_data_free_error(self):
        private = "private-memory-content-postgresql-16"
        self.enqueue()
        with channel_store.connect(str(self.path)) as conn:
            conn.execute("DROP INDEX idx_memory_index_outbox_pending")
        with self.assertRaises(consumer.MemoryIndexOutboxConsumerError) as raised:
            consumer.peek_pending_batch_v1(self.path)
        self.assertEqual(
            raised.exception.category,
            "memory_index_outbox_read_failed",
        )
        self.assertNotIn(private, str(raised.exception))
        self.assertNotIn(private, repr(raised.exception))

    def test_receipts_hide_watermark_path_and_module_never_prunes(self):
        self.enqueue()
        first = consumer.peek_pending_batch_v1(self.path)
        consumer.complete_pending_batch_v1(
            self.path,
            first,
            completed_at=COMPLETED,
        )
        self.enqueue("2026-09-06T10:02:00+00:00")
        batch = consumer.peek_pending_batch_v1(self.path)
        rendered = repr(batch)
        self.assertNotIn(str(batch.high_watermark), rendered)
        self.assertNotIn(str(self.path), rendered)
        source = Path(consumer.__file__).read_text(encoding="utf-8")
        self.assertNotIn("DELETE FROM memory_index_outbox", source)


if __name__ == "__main__":
    unittest.main()
