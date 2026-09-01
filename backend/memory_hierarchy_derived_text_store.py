"""Disposable SQLite cache for Phase 4D-B6 hierarchy derived text.

This cache is deliberately separate from the content-free hierarchy sidecar.
It may contain model-derived Episode / Topic / Canonical-State text, but every
row is bound to one exact hierarchy ``projection_digest`` and can be deleted at
any time.  It owns no Atomic Memory truth, provenance, approval/suppression
state, Runtime Authority, retrieval authority, or connection to ``relay.db``.

Fresh reads require a current server-owned node binding.  A digest mismatch is
reported as a cache miss, not as usable stale text.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import memory_hierarchy_derived_text as derived


CACHE_SCHEMA_VERSION: Final = 1
CACHE_CONTRACT_VERSION: Final = "memory-hierarchy-derived-text-cache-v1"
_MAX_PATH_CHARS: Final = 4096
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_NODE_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_NODE_TYPES: Final = frozenset({"topic", "episode", "canonical_state"})

_ERROR_CATEGORIES: Final = frozenset({
    "derived_text_cache_schema_invalid",
    "derived_text_cache_state_invalid",
    "derived_text_cache_storage_unavailable",
    "derived_text_cache_write_failed",
    "invalid_current_bindings",
    "invalid_derived_text_cache_path",
    "invalid_derived_text_document",
    "memory_hierarchy_derived_text_store_error",
})

_EXPECTED_COLUMNS: Final = {
    "derived_text_meta": (
        "singleton",
        "schema_version",
        "cache_contract_version",
        "derived_text_contract_version",
    ),
    "derived_texts": (
        "node_key",
        "node_type",
        "parent_key",
        "projection_digest",
        "content_digest",
        "text_content",
        "sentence_count",
    ),
    "derived_text_sentences": (
        "node_key",
        "ordinal",
        "sentence_text",
    ),
    "derived_text_supports": (
        "node_key",
        "sentence_ordinal",
        "support_ordinal",
        "memory_key",
    ),
}


class MemoryHierarchyDerivedTextStoreError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_derived_text_store_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_derived_text_store_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchyDerivedTextStoreError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchyDerivedTextStoreError(category)


@dataclass(frozen=True, slots=True, repr=False)
class DerivedTextCacheSnapshotV1:
    schema_version: int
    cache_contract_version: str
    derived_text_contract_version: str
    documents: tuple[derived.DerivedTextDocumentV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"<DerivedTextCacheSnapshotV1 documents={len(self.documents)}>"


def _validated_path(raw_path: object, *, must_exist: bool) -> Path:
    if not isinstance(raw_path, (str, Path)):
        _raise("invalid_derived_text_cache_path")
    try:
        path = Path(raw_path)
        text = str(path)
    except (TypeError, ValueError, OSError):
        _raise("invalid_derived_text_cache_path")
    if (
        not text
        or len(text) > _MAX_PATH_CHARS
        or "\x00" in text
        or path.name in {"", ".", ".."}
    ):
        _raise("invalid_derived_text_cache_path")
    try:
        if not path.parent.is_dir():
            _raise("invalid_derived_text_cache_path")
        if path.exists() and not path.is_file():
            _raise("invalid_derived_text_cache_path")
        if must_exist and not path.is_file():
            _raise("derived_text_cache_storage_unavailable")
    except MemoryHierarchyDerivedTextStoreError:
        raise
    except OSError:
        _raise("derived_text_cache_storage_unavailable")
    return path


def _connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error:
        _raise("derived_text_cache_storage_unavailable")


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
        conn.execute("PRAGMA query_only=ON")
        return conn
    except (OSError, sqlite3.Error):
        _raise("derived_text_cache_storage_unavailable")


def _user_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        """SELECT name FROM sqlite_master
             WHERE type='table' AND name NOT LIKE 'sqlite_%'
             ORDER BY name"""
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(str(row[1]) for row in rows)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE derived_text_meta(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL CHECK(schema_version=1),
            cache_contract_version TEXT NOT NULL,
            derived_text_contract_version TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE derived_texts(
            node_key TEXT PRIMARY KEY,
            node_type TEXT NOT NULL CHECK(
                node_type IN ('topic','episode','canonical_state')
            ),
            parent_key TEXT NOT NULL,
            projection_digest TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            text_content TEXT NOT NULL,
            sentence_count INTEGER NOT NULL CHECK(sentence_count BETWEEN 1 AND 8)
        )"""
    )
    conn.execute(
        """CREATE TABLE derived_text_sentences(
            node_key TEXT NOT NULL REFERENCES derived_texts(node_key)
                ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal>=0),
            sentence_text TEXT NOT NULL,
            PRIMARY KEY(node_key, ordinal)
        )"""
    )
    conn.execute(
        """CREATE TABLE derived_text_supports(
            node_key TEXT NOT NULL,
            sentence_ordinal INTEGER NOT NULL,
            support_ordinal INTEGER NOT NULL CHECK(support_ordinal>=0),
            memory_key TEXT NOT NULL,
            PRIMARY KEY(node_key, sentence_ordinal, support_ordinal),
            UNIQUE(node_key, sentence_ordinal, memory_key),
            FOREIGN KEY(node_key, sentence_ordinal)
                REFERENCES derived_text_sentences(node_key, ordinal)
                ON DELETE CASCADE
        )"""
    )
    conn.execute(
        """CREATE INDEX derived_text_supports_memory_idx
            ON derived_text_supports(memory_key,node_key)"""
    )
    conn.execute(
        """INSERT INTO derived_text_meta
           (singleton,schema_version,cache_contract_version,
            derived_text_contract_version)
           VALUES(1,?,?,?)""",
        (
            CACHE_SCHEMA_VERSION,
            CACHE_CONTRACT_VERSION,
            derived.DERIVED_TEXT_CONTRACT_VERSION,
        ),
    )


def _validate_schema(conn: sqlite3.Connection) -> sqlite3.Row:
    try:
        if _user_tables(conn) != tuple(sorted(_EXPECTED_COLUMNS)):
            _raise("derived_text_cache_schema_invalid")
        for table, columns in _EXPECTED_COLUMNS.items():
            if _table_columns(conn, table) != columns:
                _raise("derived_text_cache_schema_invalid")
        meta_rows = conn.execute("SELECT * FROM derived_text_meta").fetchall()
        if len(meta_rows) != 1:
            _raise("derived_text_cache_schema_invalid")
        meta = meta_rows[0]
        if (
            meta["singleton"] != 1
            or meta["schema_version"] != CACHE_SCHEMA_VERSION
            or meta["cache_contract_version"] != CACHE_CONTRACT_VERSION
            or meta["derived_text_contract_version"]
            != derived.DERIVED_TEXT_CONTRACT_VERSION
        ):
            _raise("derived_text_cache_schema_invalid")
        return meta
    except MemoryHierarchyDerivedTextStoreError:
        raise
    except sqlite3.Error:
        _raise("derived_text_cache_schema_invalid")


def initialize_derived_text_cache(raw_path: object) -> Path:
    path = _validated_path(raw_path, must_exist=False)
    conn = _connect(path)
    try:
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
        except MemoryHierarchyDerivedTextStoreError:
            raise
        except sqlite3.Error:
            _raise("derived_text_cache_storage_unavailable")
    finally:
        conn.close()
    return path


def _validate_document_against_binding(
    raw_document: object,
    raw_binding: object,
) -> tuple[
    derived.DerivedTextDocumentV1,
    derived.DerivedTextNodeBindingV1,
]:
    try:
        binding = derived._validate_binding(raw_binding)
    except derived.MemoryHierarchyDerivedTextError:
        _raise("invalid_current_bindings")
    if type(raw_document) is not derived.DerivedTextDocumentV1:
        _raise("invalid_derived_text_document")
    if (
        raw_document.contract_version != derived.DERIVED_TEXT_CONTRACT_VERSION
        or raw_document.node_type != binding.node_type
        or raw_document.node_key != binding.node_key
        or raw_document.parent_key != binding.parent_key
        or raw_document.projection_digest != binding.projection_digest
        or type(raw_document.sentences) is not tuple
        or type(raw_document.text) is not str
        or type(raw_document.content_digest) is not str
        or _DIGEST_PATTERN.fullmatch(raw_document.content_digest) is None
    ):
        _raise("invalid_derived_text_document")
    try:
        sentences = derived._validate_sentences(raw_document.sentences, binding)
    except derived.MemoryHierarchyDerivedTextError:
        _raise("invalid_derived_text_document")
    text = " ".join(sentence.text for sentence in sentences)
    if text != raw_document.text or not text or len(text) > derived.MAX_TEXT_CHARS:
        _raise("invalid_derived_text_document")
    expected_digest = derived._content_digest(binding, sentences, text)
    if expected_digest != raw_document.content_digest:
        _raise("invalid_derived_text_document")
    return raw_document, binding


def store_derived_text(
    raw_path: object,
    raw_binding: object,
    raw_document: object,
) -> derived.DerivedTextDocumentV1:
    """Atomically replace one node's cache entry after exact digest reproof."""

    path = _validated_path(raw_path, must_exist=True)
    document, binding = _validate_document_against_binding(
        raw_document,
        raw_binding,
    )
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _validate_schema(conn)
            conn.execute(
                """INSERT INTO derived_texts
                   (node_key,node_type,parent_key,projection_digest,
                    content_digest,text_content,sentence_count)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(node_key) DO UPDATE SET
                       node_type=excluded.node_type,
                       parent_key=excluded.parent_key,
                       projection_digest=excluded.projection_digest,
                       content_digest=excluded.content_digest,
                       text_content=excluded.text_content,
                       sentence_count=excluded.sentence_count""",
                (
                    binding.node_key,
                    binding.node_type,
                    binding.parent_key,
                    binding.projection_digest,
                    document.content_digest,
                    document.text,
                    len(document.sentences),
                ),
            )
            conn.execute(
                "DELETE FROM derived_text_sentences WHERE node_key=?",
                (binding.node_key,),
            )
            for sentence_ordinal, sentence in enumerate(document.sentences):
                conn.execute(
                    """INSERT INTO derived_text_sentences
                       (node_key,ordinal,sentence_text) VALUES(?,?,?)""",
                    (binding.node_key, sentence_ordinal, sentence.text),
                )
                conn.executemany(
                    """INSERT INTO derived_text_supports
                       (node_key,sentence_ordinal,support_ordinal,memory_key)
                       VALUES(?,?,?,?)""",
                    (
                        (
                            binding.node_key,
                            sentence_ordinal,
                            support_ordinal,
                            memory_key,
                        )
                        for support_ordinal, memory_key
                        in enumerate(sentence.support_keys)
                    ),
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
    except MemoryHierarchyDerivedTextStoreError:
        raise
    except sqlite3.Error:
        _raise("derived_text_cache_write_failed")
    except Exception:
        _raise("derived_text_cache_write_failed")
    finally:
        conn.close()
    return document


def _load_document_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    binding: derived.DerivedTextNodeBindingV1,
) -> derived.DerivedTextDocumentV1:
    try:
        sentence_rows = conn.execute(
            """SELECT ordinal,sentence_text FROM derived_text_sentences
                WHERE node_key=? ORDER BY ordinal""",
            (row["node_key"],),
        ).fetchall()
        if len(sentence_rows) != row["sentence_count"]:
            _raise("derived_text_cache_state_invalid")
        if tuple(int(item["ordinal"]) for item in sentence_rows) != tuple(
            range(len(sentence_rows))
        ):
            _raise("derived_text_cache_state_invalid")
        sentences: list[derived.DerivedTextSentenceV1] = []
        for sentence_row in sentence_rows:
            support_rows = conn.execute(
                """SELECT support_ordinal,memory_key FROM derived_text_supports
                    WHERE node_key=? AND sentence_ordinal=?
                    ORDER BY support_ordinal""",
                (row["node_key"], sentence_row["ordinal"]),
            ).fetchall()
            if not support_rows or tuple(
                int(item["support_ordinal"]) for item in support_rows
            ) != tuple(range(len(support_rows))):
                _raise("derived_text_cache_state_invalid")
            sentences.append(derived.DerivedTextSentenceV1(
                text=str(sentence_row["sentence_text"]),
                support_keys=tuple(str(item["memory_key"]) for item in support_rows),
            ))
        document = derived.DerivedTextDocumentV1(
            contract_version=derived.DERIVED_TEXT_CONTRACT_VERSION,
            node_type=str(row["node_type"]),
            node_key=str(row["node_key"]),
            parent_key=str(row["parent_key"]),
            projection_digest=str(row["projection_digest"]),
            content_digest=str(row["content_digest"]),
            text=str(row["text_content"]),
            sentences=tuple(sentences),
        )
        validated, _ = _validate_document_against_binding(document, binding)
        return validated
    except MemoryHierarchyDerivedTextStoreError:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        _raise("derived_text_cache_state_invalid")


def load_fresh_derived_text(
    raw_path: object,
    raw_binding: object,
) -> derived.DerivedTextDocumentV1 | None:
    """Load one document only when its binding equals the current node revision."""

    path = _validated_path(raw_path, must_exist=True)
    try:
        binding = derived._validate_binding(raw_binding)
    except derived.MemoryHierarchyDerivedTextError:
        _raise("invalid_current_bindings")
    conn = _connect_readonly(path)
    try:
        _validate_schema(conn)
        rows = conn.execute(
            "SELECT * FROM derived_texts WHERE node_key=?",
            (binding.node_key,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            _raise("derived_text_cache_state_invalid")
        row = rows[0]
        if (
            row["node_type"] != binding.node_type
            or row["parent_key"] != binding.parent_key
            or row["projection_digest"] != binding.projection_digest
        ):
            return None
        return _load_document_row(conn, row, binding)
    except MemoryHierarchyDerivedTextStoreError:
        raise
    except sqlite3.Error:
        _raise("derived_text_cache_storage_unavailable")
    finally:
        conn.close()


def prune_derived_text_cache(
    raw_path: object,
    raw_current_bindings: object,
) -> int:
    """Delete entries for obsolete or changed hierarchy node revisions."""

    path = _validated_path(raw_path, must_exist=True)
    if type(raw_current_bindings) not in (list, tuple):
        _raise("invalid_current_bindings")
    current: dict[str, derived.DerivedTextNodeBindingV1] = {}
    for raw in raw_current_bindings:
        try:
            binding = derived._validate_binding(raw)
        except derived.MemoryHierarchyDerivedTextError:
            _raise("invalid_current_bindings")
        if binding.node_key in current:
            _raise("invalid_current_bindings")
        current[binding.node_key] = binding

    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _validate_schema(conn)
            rows = conn.execute(
                """SELECT node_key,node_type,parent_key,projection_digest
                     FROM derived_texts ORDER BY node_key"""
            ).fetchall()
            stale: list[str] = []
            for row in rows:
                binding = current.get(str(row["node_key"]))
                if binding is None or (
                    row["node_type"] != binding.node_type
                    or row["parent_key"] != binding.parent_key
                    or row["projection_digest"] != binding.projection_digest
                ):
                    stale.append(str(row["node_key"]))
            conn.executemany(
                "DELETE FROM derived_texts WHERE node_key=?",
                ((node_key,) for node_key in stale),
            )
            conn.execute("COMMIT")
            return len(stale)
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
    except MemoryHierarchyDerivedTextStoreError:
        raise
    except sqlite3.Error:
        _raise("derived_text_cache_write_failed")
    finally:
        conn.close()
