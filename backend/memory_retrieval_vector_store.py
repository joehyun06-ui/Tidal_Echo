"""Disposable float32 vector sidecar for Phase 4D-C3.

The sidecar stores only current-revision normal/global-user embedding vectors and
server binding metadata.  It stores no Atomic plaintext, sensitive/restricted
Memory, provenance, hierarchy summary text, prompt state, or Memory authority.
It is exact-schema validated, replaceable, and safe to delete/rebuild.
"""

from __future__ import annotations

import re
import sqlite3
import struct
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import memory_retrieval_vector as vector


VECTOR_STORE_SCHEMA_VERSION: Final = 1
VECTOR_STORE_CONTRACT_VERSION: Final = "memory-retrieval-vector-store-v1"
_MAX_PATH_CHARS: Final = 4096
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_MODEL_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")

_ERROR_CATEGORIES: Final = frozenset({
    "vector_index_invalid",
    "vector_index_path_invalid",
    "vector_index_schema_invalid",
    "vector_index_storage_unavailable",
    "vector_index_write_failed",
    "memory_retrieval_vector_store_error",
})

_EXPECTED_COLUMNS: Final = {
    "vector_meta": (
        "singleton",
        "schema_version",
        "store_contract_version",
        "vector_contract_version",
        "embedding_contract_version",
        "generation",
        "source_snapshot_digest",
        "embedding_model",
        "dimensions",
        "document_count",
    ),
    "vector_documents": (
        "memory_key",
        "atomic_revision_digest",
        "vector_blob",
        "generation",
    ),
}


class MemoryRetrievalVectorStoreError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_retrieval_vector_store_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_retrieval_vector_store_error"

    def __repr__(self) -> str:
        return f"MemoryRetrievalVectorStoreError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryRetrievalVectorStoreError(category)


@dataclass(frozen=True, slots=True, repr=False)
class VectorStoreSnapshotV1:
    generation: int
    plan: vector.VectorIndexPlanV1 = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<VectorStoreSnapshotV1 "
            f"generation={self.generation} documents={self.plan.document_count} "
            f"dimensions={self.plan.dimensions}>"
        )


def _validated_path(
    raw_path: object,
    *,
    must_exist: bool,
    forbidden_paths: object = (),
) -> Path:
    if not isinstance(raw_path, (str, Path)):
        _raise("vector_index_path_invalid")
    try:
        path = Path(raw_path)
        text = str(path)
        resolved = path.resolve(strict=False)
    except (OSError, TypeError, ValueError):
        _raise("vector_index_path_invalid")
    if (
        not text
        or len(text) > _MAX_PATH_CHARS
        or "\x00" in text
        or path.name in {"", ".", ".."}
    ):
        _raise("vector_index_path_invalid")
    try:
        forbidden = tuple(Path(item).resolve(strict=False) for item in forbidden_paths)
    except (OSError, TypeError, ValueError):
        _raise("vector_index_path_invalid")
    if resolved in forbidden:
        _raise("vector_index_path_invalid")
    try:
        if not path.parent.is_dir():
            _raise("vector_index_path_invalid")
        if path.exists() and not path.is_file():
            _raise("vector_index_path_invalid")
        if must_exist and not path.is_file():
            _raise("vector_index_storage_unavailable")
    except MemoryRetrievalVectorStoreError:
        raise
    except OSError:
        _raise("vector_index_storage_unavailable")
    return path


def _connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error:
        _raise("vector_index_storage_unavailable")


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
        return conn
    except (OSError, sqlite3.Error):
        _raise("vector_index_storage_unavailable")


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
        """CREATE TABLE vector_meta(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL CHECK(schema_version=1),
            store_contract_version TEXT NOT NULL,
            vector_contract_version TEXT NOT NULL,
            embedding_contract_version TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>=0),
            source_snapshot_digest TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            dimensions INTEGER NOT NULL CHECK(dimensions>=0),
            document_count INTEGER NOT NULL CHECK(document_count>=0)
        )"""
    )
    conn.execute(
        """CREATE TABLE vector_documents(
            memory_key TEXT PRIMARY KEY,
            atomic_revision_digest TEXT NOT NULL,
            vector_blob BLOB NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>0)
        )"""
    )
    conn.execute(
        """INSERT INTO vector_meta(
            singleton,schema_version,store_contract_version,
            vector_contract_version,embedding_contract_version,generation,
            source_snapshot_digest,embedding_model,dimensions,document_count
        ) VALUES(1,?,?,?,?,?,?,?,?,?)""",
        (
            VECTOR_STORE_SCHEMA_VERSION,
            VECTOR_STORE_CONTRACT_VERSION,
            vector.VECTOR_CONTRACT_VERSION,
            vector.EMBEDDING_CONTRACT_VERSION,
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
            _raise("vector_index_schema_invalid")
        for table, columns in _EXPECTED_COLUMNS.items():
            if _table_columns(conn, table) != columns:
                _raise("vector_index_schema_invalid")
        rows = conn.execute("SELECT * FROM vector_meta").fetchall()
        if len(rows) != 1:
            _raise("vector_index_schema_invalid")
        meta = rows[0]
        generation = meta["generation"]
        digest = meta["source_snapshot_digest"]
        model = meta["embedding_model"]
        dimensions = meta["dimensions"]
        document_count = meta["document_count"]
        if (
            meta["singleton"] != 1
            or meta["schema_version"] != VECTOR_STORE_SCHEMA_VERSION
            or meta["store_contract_version"] != VECTOR_STORE_CONTRACT_VERSION
            or meta["vector_contract_version"] != vector.VECTOR_CONTRACT_VERSION
            or meta["embedding_contract_version"] != vector.EMBEDDING_CONTRACT_VERSION
            or type(generation) is not int
            or generation < 0
            or type(dimensions) is not int
            or dimensions < 0
            or type(document_count) is not int
            or document_count < 0
            or type(digest) is not str
            or type(model) is not str
        ):
            _raise("vector_index_schema_invalid")
        if generation == 0:
            if digest or model or dimensions or document_count:
                _raise("vector_index_schema_invalid")
        elif (
            _DIGEST_PATTERN.fullmatch(digest) is None
            or _MODEL_PATTERN.fullmatch(model) is None
            or not vector.MIN_VECTOR_DIMENSIONS
            <= dimensions
            <= vector.MAX_VECTOR_DIMENSIONS
            or document_count > hierarchy_max_documents()
        ):
            _raise("vector_index_schema_invalid")
        return meta
    except MemoryRetrievalVectorStoreError:
        raise
    except sqlite3.Error:
        _raise("vector_index_schema_invalid")


def hierarchy_max_documents() -> int:
    # Kept as a function so the store has no second magic copy of the C3 bound.
    from backend import memory_hierarchy_projection as hierarchy

    return hierarchy.MAX_ATOMICS


def initialize_vector_store(
    raw_path: object,
    *,
    forbidden_paths: object = (),
) -> Path:
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
    except MemoryRetrievalVectorStoreError:
        raise
    except sqlite3.Error:
        _raise("vector_index_storage_unavailable")
    finally:
        conn.close()
    return path


def _pack_vector(values: tuple[float, ...]) -> bytes:
    try:
        return struct.pack(f"<{len(values)}f", *values)
    except (OverflowError, struct.error):
        _raise("vector_index_invalid")


def _unpack_vector(raw: object, dimensions: int) -> tuple[float, ...]:
    if type(raw) is not bytes or len(raw) != dimensions * 4:
        _raise("vector_index_schema_invalid")
    try:
        return tuple(struct.unpack(f"<{dimensions}f", raw))
    except struct.error:
        _raise("vector_index_schema_invalid")


def apply_vector_index_plan(
    raw_path: object,
    raw_plan: object,
) -> VectorStoreSnapshotV1:
    path = _validated_path(raw_path, must_exist=True)
    try:
        plan = vector.validate_vector_index_plan_v1(raw_plan)
    except vector.MemoryRetrievalVectorError:
        _raise("vector_index_invalid")
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            meta = _validate_schema(conn)
            generation = int(meta["generation"]) + 1
            conn.execute("DELETE FROM vector_documents")
            conn.executemany(
                """INSERT INTO vector_documents(
                    memory_key,atomic_revision_digest,vector_blob,generation
                ) VALUES(?,?,?,?)""",
                (
                    (
                        document.memory_key,
                        document.atomic_revision_digest,
                        _pack_vector(document.vector),
                        generation,
                    )
                    for document in plan.documents
                ),
            )
            conn.execute(
                """UPDATE vector_meta
                      SET generation=?,source_snapshot_digest=?,embedding_model=?,
                          dimensions=?,document_count=?
                    WHERE singleton=1""",
                (
                    generation,
                    plan.source_snapshot_digest,
                    plan.embedding_model,
                    plan.dimensions,
                    plan.document_count,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                _raise("vector_index_write_failed")
            _validate_schema(conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
    except MemoryRetrievalVectorStoreError:
        raise
    except sqlite3.IntegrityError:
        _raise("vector_index_write_failed")
    except (OSError, sqlite3.Error):
        _raise("vector_index_storage_unavailable")
    except Exception:
        _raise("vector_index_write_failed")
    finally:
        conn.close()
    snapshot = load_vector_store_snapshot(path)
    if snapshot.plan != plan:
        _raise("vector_index_write_failed")
    return snapshot


def load_vector_store_snapshot(raw_path: object) -> VectorStoreSnapshotV1:
    path = _validated_path(raw_path, must_exist=True)
    conn = _connect_readonly(path)
    try:
        meta = _validate_schema(conn)
        if int(meta["generation"]) == 0:
            _raise("vector_index_schema_invalid")
        rows = conn.execute(
            """SELECT memory_key,atomic_revision_digest,vector_blob,generation
                 FROM vector_documents ORDER BY memory_key"""
        ).fetchall()
        if len(rows) != int(meta["document_count"]):
            _raise("vector_index_schema_invalid")
        documents: list[vector.VectorDocumentPlanV1] = []
        for row in rows:
            if row["generation"] != meta["generation"]:
                _raise("vector_index_schema_invalid")
            memory_key = str(row["memory_key"])
            revision = str(row["atomic_revision_digest"])
            if (
                _MEMORY_KEY_PATTERN.fullmatch(memory_key) is None
                or _DIGEST_PATTERN.fullmatch(revision) is None
            ):
                _raise("vector_index_schema_invalid")
            documents.append(vector.VectorDocumentPlanV1(
                memory_key=memory_key,
                atomic_revision_digest=revision,
                vector=_unpack_vector(row["vector_blob"], int(meta["dimensions"])),
            ))
        plan = vector.VectorIndexPlanV1(
            contract_version=vector.VECTOR_CONTRACT_VERSION,
            embedding_contract_version=vector.EMBEDDING_CONTRACT_VERSION,
            source_snapshot_digest=str(meta["source_snapshot_digest"]),
            embedding_model=str(meta["embedding_model"]),
            dimensions=int(meta["dimensions"]),
            documents=tuple(documents),
        )
        try:
            validated = vector.validate_vector_index_plan_v1(plan)
        except vector.MemoryRetrievalVectorError:
            _raise("vector_index_schema_invalid")
        return VectorStoreSnapshotV1(
            generation=int(meta["generation"]),
            plan=validated,
        )
    except MemoryRetrievalVectorStoreError:
        raise
    except sqlite3.Error:
        _raise("vector_index_storage_unavailable")
    finally:
        conn.close()


def search_vector_store(
    raw_path: object,
    query_vector: object,
    *,
    expected_source_snapshot_digest: object | None = None,
    max_hits: object = vector.MAX_VECTOR_HITS,
    minimum_similarity: object = 0.0,
) -> vector.VectorSearchResultV1:
    snapshot = load_vector_store_snapshot(raw_path)
    if expected_source_snapshot_digest is not None:
        if (
            type(expected_source_snapshot_digest) is not str
            or snapshot.plan.source_snapshot_digest
            != expected_source_snapshot_digest
        ):
            _raise("vector_index_invalid")
    try:
        return vector.search_vector_index_v1(
            snapshot.plan,
            query_vector,
            max_hits=max_hits,
            minimum_similarity=minimum_similarity,
        )
    except vector.MemoryRetrievalVectorError:
        _raise("vector_index_invalid")
