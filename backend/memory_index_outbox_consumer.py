"""Content-free durable consumer for the Memory index dirty outbox.

The outbox is only a coalescing signal.  A consumer receives no Memory key,
content, query, model, or vector identity; it may acknowledge only the exact
pending prefix whose high watermark it observed before rebuilding from the
authoritative database.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import channel_store


OUTBOX_CONSUMER_CONTRACT_VERSION: Final = "memory-index-outbox-consumer-v1"

_ERROR_CATEGORIES: Final = frozenset({
    "memory_index_outbox_batch_invalid",
    "memory_index_outbox_completion_failed",
    "memory_index_outbox_configuration_invalid",
    "memory_index_outbox_read_failed",
    "memory_index_outbox_consumer_error",
})
_BATCH_SEAL: Final = object()


class MemoryIndexOutboxConsumerError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_index_outbox_consumer_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except BaseException:
            return "memory_index_outbox_consumer_error"

    def __repr__(self) -> str:
        return f"MemoryIndexOutboxConsumerError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryIndexOutboxConsumerError(category)


@dataclass(frozen=True, slots=True, repr=False)
class MemoryIndexDirtyBatchV1:
    contract_version: str
    high_watermark: int = field(repr=False)
    pending_count: int
    database_path: Path = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    def __repr__(self) -> str:
        return f"<MemoryIndexDirtyBatchV1 pending={self.pending_count}>"


@dataclass(frozen=True, slots=True, repr=False)
class MemoryIndexCompletionReceiptV1:
    contract_version: str
    completed_count: int

    def __repr__(self) -> str:
        return (
            "<MemoryIndexCompletionReceiptV1 "
            f"completed={self.completed_count}>"
        )


def _database_path(raw: object) -> Path:
    if not isinstance(raw, (str, os.PathLike)):
        _raise("memory_index_outbox_configuration_invalid")
    try:
        path = Path(raw)
        if not path.is_absolute():
            _raise("memory_index_outbox_configuration_invalid")
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            _raise("memory_index_outbox_configuration_invalid")
        return resolved
    except MemoryIndexOutboxConsumerError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        _raise("memory_index_outbox_configuration_invalid")


def peek_pending_batch_v1(
    database_path: object,
    *,
    timeout_seconds: object = 30.0,
) -> MemoryIndexDirtyBatchV1 | None:
    """Read one immutable pending-prefix watermark without opening write mode."""

    path = _database_path(database_path)
    try:
        conn = channel_store.connect_read_only(
            path,
            timeout_seconds=timeout_seconds,
        )
        try:
            conn.execute("BEGIN")
            channel_store.validate_memory_index_outbox_schema_v1_v11(conn)
            row = conn.execute(
                """SELECT COUNT(*) AS pending_count,
                          MAX(id) AS high_watermark,
                          MIN(CASE WHEN event_kind=? THEN 1 ELSE 0 END)
                              AS kinds_valid
                     FROM memory_index_outbox
                    WHERE completed_at IS NULL""",
                (channel_store.MEMORY_INDEX_DIRTY_EVENT_KIND,),
            ).fetchone()
            conn.execute("ROLLBACK")
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()
    except MemoryIndexOutboxConsumerError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        _raise("memory_index_outbox_read_failed")
    except Exception:
        _raise("memory_index_outbox_read_failed")

    try:
        count = int(row["pending_count"])
        watermark = row["high_watermark"]
        kinds_valid = row["kinds_valid"]
    except (KeyError, TypeError, ValueError):
        _raise("memory_index_outbox_read_failed")
    if count == 0:
        if watermark is not None or kinds_valid is not None:
            _raise("memory_index_outbox_read_failed")
        return None
    if (
        type(watermark) is not int
        or isinstance(watermark, bool)
        or watermark <= 0
        or count <= 0
        or count > watermark
        or kinds_valid != 1
    ):
        _raise("memory_index_outbox_read_failed")
    return MemoryIndexDirtyBatchV1(
        contract_version=OUTBOX_CONSUMER_CONTRACT_VERSION,
        high_watermark=watermark,
        pending_count=count,
        database_path=path,
        _seal=_BATCH_SEAL,
    )


def _validated_batch(
    raw: object,
    path: Path,
) -> MemoryIndexDirtyBatchV1:
    if (
        type(raw) is not MemoryIndexDirtyBatchV1
        or raw.contract_version != OUTBOX_CONSUMER_CONTRACT_VERSION
        or type(raw.high_watermark) is not int
        or isinstance(raw.high_watermark, bool)
        or raw.high_watermark <= 0
        or type(raw.pending_count) is not int
        or isinstance(raw.pending_count, bool)
        or not 1 <= raw.pending_count <= raw.high_watermark
        or not isinstance(raw.database_path, Path)
        or raw.database_path != path
        or raw._seal is not _BATCH_SEAL
    ):
        _raise("memory_index_outbox_batch_invalid")
    return raw


def complete_pending_batch_v1(
    database_path: object,
    batch: object,
    *,
    completed_at: object = None,
) -> MemoryIndexCompletionReceiptV1:
    """Monotonically acknowledge only pending rows at or below the watermark."""

    path = _database_path(database_path)
    proved = _validated_batch(batch, path)
    stamp = channel_store.now_iso() if completed_at is None else completed_at
    if type(stamp) is not str:
        _raise("memory_index_outbox_batch_invalid")

    conn = None
    try:
        conn = channel_store.connect(str(path))
        conn.execute("BEGIN IMMEDIATE")
        channel_store.validate_memory_index_outbox_schema_v1_v11(conn)
        row = conn.execute(
            """SELECT COUNT(*) AS pending_count,
                      MAX(created_at) AS latest_created_at,
                      MIN(CASE WHEN event_kind=? THEN 1 ELSE 0 END)
                          AS kinds_valid
                 FROM memory_index_outbox
                WHERE completed_at IS NULL AND id<=?""",
            (
                channel_store.MEMORY_INDEX_DIRTY_EVENT_KIND,
                proved.high_watermark,
            ),
        ).fetchone()
        pending = int(row["pending_count"])
        latest_created_at = row["latest_created_at"]
        kinds_valid = row["kinds_valid"]
        if (
            pending < 0
            or pending > proved.pending_count
            or (pending == 0 and (latest_created_at is not None or kinds_valid is not None))
            or (pending > 0 and (type(latest_created_at) is not str or kinds_valid != 1))
            or (pending > 0 and stamp < latest_created_at)
        ):
            _raise("memory_index_outbox_batch_invalid")
        conn.execute(
            """UPDATE memory_index_outbox
                  SET completed_at=?
                WHERE completed_at IS NULL AND id<=?""",
            (stamp, proved.high_watermark),
        )
        changed = int(conn.execute("SELECT changes()").fetchone()[0])
        remaining = int(conn.execute(
            """SELECT COUNT(*) FROM memory_index_outbox
                WHERE completed_at IS NULL AND id<=?""",
            (proved.high_watermark,),
        ).fetchone()[0])
        if changed != pending or remaining != 0:
            _raise("memory_index_outbox_completion_failed")
        conn.execute("COMMIT")
    except MemoryIndexOutboxConsumerError:
        if conn is not None and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        if conn is not None and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        _raise("memory_index_outbox_completion_failed")
    except Exception:
        if conn is not None and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        _raise("memory_index_outbox_completion_failed")
    finally:
        if conn is not None:
            conn.close()

    return MemoryIndexCompletionReceiptV1(
        contract_version=OUTBOX_CONSUMER_CONTRACT_VERSION,
        completed_count=changed,
    )


__all__ = (
    "MemoryIndexCompletionReceiptV1",
    "MemoryIndexDirtyBatchV1",
    "MemoryIndexOutboxConsumerError",
    "OUTBOX_CONSUMER_CONTRACT_VERSION",
    "complete_pending_batch_v1",
    "peek_pending_batch_v1",
)
