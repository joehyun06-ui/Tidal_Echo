"""Disposable SQLite store for Phase 4D-C1 BM25 routing data.

The store contains only Memory keys, document lengths, domain-separated HMAC term
hashes, term frequencies, and source revision metadata.  It never stores Atomic
Memory plaintext, plaintext lexical terms, provenance, summaries, vectors, or
Memory authority state.  It may be deleted and rebuilt from authoritative Memory.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import memory_retrieval_bm25 as bm25


BM25_STORE_SCHEMA_VERSION: Final = 1
BM25_STORE_CONTRACT_VERSION: Final = "memory-retrieval-bm25-store-v1"
_MAX_PATH_CHARS: Final = 4096
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_KEY_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

_ERROR_CATEGORIES: Final = frozenset({
    "bm25_index_invalid",
    "bm25_index_path_invalid",
    "bm25_index_schema_invalid",
    "bm25_index_storage_unavailable",
    "bm25_index_write_failed",
    "memory_retrieval_bm25_store_error",
})

_EXPECTED_COLUMNS: Final = {
    "bm25_meta": (
        "singleton",
        "schema_version",
        "contract_version",
        "bm25_contract_version",
        "tokenizer_version",
        "generation",
        "source_snapshot_digest",
        "term_key_id",
        "document_count",
        "total_document_length",
    ),
    "bm25_documents": (
        "memory_key",
        "document_length",
        "generation",
    ),
    "bm25_postings": (
        "term_hash",
        "memory_key",
        "term_frequency",
    ),
}


class MemoryRetrievalBM25StoreError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_bm25_store_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_retrieval_bm25_store_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalBM25StoreError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalBM25StoreError(category)


@dataclass(frozen=True, slots=True, repr=False)
class BM25StoreSnapshotV1:
    generation: int
    plan: bm25.BM25IndexPlanV1 = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<BM25StoreSnapshotV1 "
            f"generation={self.generation} documents={self.plan.document_count} "
            f"terms={self.plan.unique_term_count}>"
        )


def _validated_path(
    raw_path: object,
    *,
    must_exist: bool,
    forbidden_paths: object = (),
) -> Path:
    if not isinstance(raw_path, (str, Path)):
        _raise("bm25_index_path_invalid")
    try:
        path = Path(raw_path)
        text = str(path)
        resolved = path.resolve(strict=False)
    except (TypeError, ValueError, OSError):
        _raise("bm25_index_path_invalid")
    if (
        not text
        or len(text) > _MAX_PATH_CHARS
        or "\x00" in text
        or path.name in {"", ".", ".."}
    ):
        _raise("bm25_index_path_invalid")
    try:
        forbidden = tuple(Path(item).resolve(strict=False) for item in forbidden_paths)
    except (TypeError, ValueError, OSError):
        _raise("bm25_index_path_invalid")
    if resolved in forbidden:
        _raise("bm25_index_path_invalid")
    try:
        if not path.parent.is_dir():
            _raise("bm25_index_path_invalid")
        if path.exists() and not path.is_file():
            _raise("bm25_index_path_invalid")
        if must_exist and not path.is_file():
            _raise("bm25_index_storage_unavailable")
    except MemoryRetrievalBM25StoreError:
        raise
    except OSError:
        _raise("bm25_index_storage_unavailable")
    return path


def _connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error:
        _raise("bm25_index_storage_unavailable")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        quoted = urllib.parse.quote(str(path.resolve()), safe="/:")
        conn = sqlite3.connect(
            f"file:{quoted}?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except (OSError, sqlite3.Error):
        _raise("bm25_index_storage_unavailable")


def _user_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        """SELECT name FROM sqlite_master
             WHERE type='table' AND name NOT LIKE 'sqlite_%'
             ORDER BY name"""
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall())


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE bm25_meta(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL CHECK(schema_version=1),
            contract_version TEXT NOT NULL,
            bm25_contract_version TEXT NOT NULL,
            tokenizer_version TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>=0),
            source_snapshot_digest TEXT NOT NULL,
            term_key_id TEXT NOT NULL,
            document_count INTEGER NOT NULL CHECK(document_count>=0),
            total_document_length INTEGER NOT NULL CHECK(total_document_length>=0)
        )"""
    )
    conn.execute(
        """CREATE TABLE bm25_documents(
            memory_key TEXT PRIMARY KEY,
            document_length INTEGER NOT NULL CHECK(document_length>0),
            generation INTEGER NOT NULL CHECK(generation>0)
        )"""
    )
    conn.execute(
        """CREATE TABLE bm25_postings(
            term_hash BLOB NOT NULL,
            memory_key TEXT NOT NULL REFERENCES bm25_documents(memory_key)
                ON DELETE CASCADE,
            term_frequency INTEGER NOT NULL CHECK(term_frequency>0),
            PRIMARY KEY(term_hash,memory_key)
        )"""
    )
    conn.execute(
        """CREATE INDEX bm25_postings_memory_idx
            ON bm25_postings(memory_key,term_hash)"""
    )
    conn.execute(
        """INSERT INTO bm25_meta(
            singleton,schema_version,contract_version,bm25_contract_version,
            tokenizer_version,generation,source_snapshot_digest,term_key_id,
            document_count,total_document_length
        ) VALUES(1,?,?,?,?,?,?,?,?,?)""",
        (
            BM25_STORE_SCHEMA_VERSION,
            BM25_STORE_CONTRACT_VERSION,
            bm25.BM25_CONTRACT_VERSION,
            bm25.TOKENIZER_VERSION,
            0,
            "",
            "",
            0,
            0,
        ),
    )


def _validate_schema(conn: sqlite3.Connection) -> sqlite3.Row:
    try:
        if _user_tables(conn) != tuple(sorted(_EXPECTED_COLUMNS)):
            _raise("bm25_index_schema_invalid")
        for table, columns in _EXPECTED_COLUMNS.items():
            if _table_columns(conn, table) != columns:
                _raise("bm25_index_schema_invalid")
        foreign = conn.execute("PRAGMA foreign_key_list(bm25_postings)").fetchall()
        if len(foreign) != 1 or foreign[0]["table"] != "bm25_documents":
            _raise("bm25_index_schema_invalid")
        rows = conn.execute("SELECT * FROM bm25_meta").fetchall()
        if len(rows) != 1:
            _raise("bm25_index_schema_invalid")
        meta = rows[0]
        generation = meta["generation"]
        digest = meta["source_snapshot_digest"]
        key_id = meta["term_key_id"]
        document_count = meta["document_count"]
        total_length = meta["total_document_length"]
        if (
            meta["singleton"] != 1
            or meta["schema_version"] != BM25_STORE_SCHEMA_VERSION
            or meta["contract_version"] != BM25_STORE_CONTRACT_VERSION
            or meta["bm25_contract_version"] != bm25.BM25_CONTRACT_VERSION
            or meta["tokenizer_version"] != bm25.TOKENIZER_VERSION
            or type(generation) is not int
            or generation < 0
            or type(document_count) is not int
            or document_count < 0
            or type(total_length) is not int
            or total_length < 0
            or type(digest) is not str
            or type(key_id) is not str
        ):
            _raise("bm25_index_schema_invalid")
        if generation == 0:
            if digest or key_id or document_count or total_length:
                _raise("bm25_index_schema_invalid")
        elif (
            _DIGEST_PATTERN.fullmatch(digest) is None
            or _KEY_ID_PATTERN.fullmatch(key_id) is None
        ):
            _raise("bm25_index_schema_invalid")
        return meta
    except MemoryRetrievalBM25StoreError:
        raise
    except sqlite3.Error:
        _raise("bm25_index_schema_invalid")


def initialize_bm25_store(
    raw_path: object,
    *,
    forbidden_paths: object = (),
) -> Path:
    """Create an empty disposable index or validate an existing exact schema."""

    path = _validated_path(
        raw_path,
        must_exist=False,
        forbidden_paths=forbidden_paths,
    )
    conn = _connect(path)
    try:
        tables = _user_tables(conn)
        if not tables:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _create_schema(conn)
                _validate_schema(conn)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        else:
            _validate_schema(conn)
    except MemoryRetrievalBM25StoreError:
        raise
    except sqlite3.Error:
        _raise("bm25_index_storage_unavailable")
    finally:
        conn.close()
    return path


def apply_bm25_index_plan(
    raw_path: object,
    raw_plan: object,
) -> BM25StoreSnapshotV1:
    """Atomically replace the complete disposable lexical index."""

    path = _validated_path(raw_path, must_exist=True)
    try:
        plan = bm25.validate_bm25_index_plan_v1(raw_plan)
    except bm25.MemoryRetrievalBM25Error:
        _raise("bm25_index_invalid")

    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            meta = _validate_schema(conn)
            generation = int(meta["generation"]) + 1
            conn.execute("DELETE FROM bm25_postings")
            conn.execute("DELETE FROM bm25_documents")
            for document in plan.documents:
                conn.execute(
                    """INSERT INTO bm25_documents(
                        memory_key,document_length,generation
                    ) VALUES(?,?,?)""",
                    (
                        document.memory_key,
                        document.document_length,
                        generation,
                    ),
                )
                conn.executemany(
                    """INSERT INTO bm25_postings(
                        term_hash,memory_key,term_frequency
                    ) VALUES(?,?,?)""",
                    (
                        (
                            posting.term_hash,
                            document.memory_key,
                            posting.term_frequency,
                        )
                        for posting in document.postings
                    ),
                )
            conn.execute(
                """UPDATE bm25_meta
                      SET generation=?,source_snapshot_digest=?,term_key_id=?,
                          document_count=?,total_document_length=?
                    WHERE singleton=1""",
                (
                    generation,
                    plan.source_snapshot_digest,
                    plan.term_key_id,
                    plan.document_count,
                    plan.total_document_length,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                _raise("bm25_index_write_failed")
            _validate_schema(conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
    except MemoryRetrievalBM25StoreError:
        raise
    except sqlite3.IntegrityError:
        _raise("bm25_index_write_failed")
    except (OSError, sqlite3.Error):
        _raise("bm25_index_storage_unavailable")
    except Exception:
        _raise("bm25_index_write_failed")
    finally:
        conn.close()

    snapshot = load_bm25_store_snapshot(path)
    if snapshot.plan != plan:
        _raise("bm25_index_write_failed")
    return snapshot


def _plan_from_connection(
    conn: sqlite3.Connection,
    meta: sqlite3.Row,
) -> bm25.BM25IndexPlanV1:
    documents: list[bm25.BM25DocumentPlanV1] = []
    rows = conn.execute(
        """SELECT memory_key,document_length,generation
             FROM bm25_documents ORDER BY memory_key"""
    ).fetchall()
    if len(rows) != int(meta["document_count"]):
        _raise("bm25_index_schema_invalid")
    for row in rows:
        if row["generation"] != meta["generation"]:
            _raise("bm25_index_schema_invalid")
        posting_rows = conn.execute(
            """SELECT term_hash,term_frequency FROM bm25_postings
                WHERE memory_key=? ORDER BY term_hash""",
            (row["memory_key"],),
        ).fetchall()
        try:
            postings = tuple(
                bm25.BM25PostingPlanV1(
                    bytes(posting["term_hash"]),
                    int(posting["term_frequency"]),
                )
                for posting in posting_rows
            )
        except (TypeError, ValueError):
            _raise("bm25_index_schema_invalid")
        documents.append(bm25.BM25DocumentPlanV1(
            memory_key=str(row["memory_key"]),
            document_length=int(row["document_length"]),
            postings=postings,
        ))
    df_rows = conn.execute(
        """SELECT term_hash,COUNT(*) AS document_frequency
             FROM bm25_postings GROUP BY term_hash ORDER BY term_hash"""
    ).fetchall()
    try:
        frequencies = tuple(
            (bytes(row["term_hash"]), int(row["document_frequency"]))
            for row in df_rows
        )
    except (TypeError, ValueError):
        _raise("bm25_index_schema_invalid")
    plan = bm25.BM25IndexPlanV1(
        contract_version=bm25.BM25_CONTRACT_VERSION,
        tokenizer_version=bm25.TOKENIZER_VERSION,
        source_snapshot_digest=str(meta["source_snapshot_digest"]),
        term_key_id=str(meta["term_key_id"]),
        documents=tuple(documents),
        term_document_frequencies=frequencies,
    )
    try:
        validated = bm25.validate_bm25_index_plan_v1(plan)
    except bm25.MemoryRetrievalBM25Error:
        _raise("bm25_index_schema_invalid")
    if validated.total_document_length != int(meta["total_document_length"]):
        _raise("bm25_index_schema_invalid")
    return validated


def load_bm25_store_snapshot(raw_path: object) -> BM25StoreSnapshotV1:
    path = _validated_path(raw_path, must_exist=True)
    conn = _connect_readonly(path)
    try:
        meta = _validate_schema(conn)
        if int(meta["generation"]) == 0:
            _raise("bm25_index_schema_invalid")
        plan = _plan_from_connection(conn, meta)
        return BM25StoreSnapshotV1(
            generation=int(meta["generation"]),
            plan=plan,
        )
    except MemoryRetrievalBM25StoreError:
        raise
    except sqlite3.Error:
        _raise("bm25_index_storage_unavailable")
    finally:
        conn.close()


def search_bm25_store(
    raw_path: object,
    query_text: object,
    *,
    term_key_id: object,
    term_hmac_secret: object,
    expected_source_snapshot_digest: object | None = None,
    max_hits: object = bm25.MAX_HITS,
) -> bm25.BM25SearchResultV1:
    """Read one current index snapshot and search it without returning plaintext."""

    snapshot = load_bm25_store_snapshot(raw_path)
    if expected_source_snapshot_digest is not None:
        if (
            type(expected_source_snapshot_digest) is not str
            or expected_source_snapshot_digest
            != snapshot.plan.source_snapshot_digest
        ):
            _raise("bm25_index_invalid")
    try:
        return bm25.search_bm25_index_v1(
            snapshot.plan,
            query_text,
            term_key_id=term_key_id,
            term_hmac_secret=term_hmac_secret,
            max_hits=max_hits,
        )
    except bm25.MemoryRetrievalBM25Error:
        _raise("bm25_index_invalid")
