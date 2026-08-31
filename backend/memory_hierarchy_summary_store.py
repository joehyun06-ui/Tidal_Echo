"""Disposable SQLite cache for Phase 4D-B derived hierarchy text.

Unlike the content-free hierarchy sidecar, this cache intentionally stores
validated derived summary clauses.  It still owns no Memory truth or write
authority: every cache entry is bound to one exact hierarchy node type/key and
projection digest, and reads require the caller to provide the current node.
A missing or mismatched projection revision returns no text.

The cache never opens the authoritative relay database or hierarchy sidecar.  A
composition layer must pass their paths as forbidden aliases during cache
initialization and must supply current proved hierarchy nodes on write/read.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import (
    memory_hierarchy_projection as hierarchy,
    memory_hierarchy_summary as summary,
    memory_policy,
)


SUMMARY_STORE_SCHEMA_VERSION: Final = 1
SUMMARY_STORE_CONTRACT_VERSION: Final = "memory-hierarchy-summary-store-v1"
_MAX_PATH_CHARS: Final = 4096
_NODE_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_SUPPORTED_NODE_TYPES: Final = frozenset({"topic", "canonical_state"})

_ERROR_CATEGORIES: Final = frozenset({
    "invalid_current_summary_node",
    "invalid_summary_cache_entry",
    "invalid_summary_store_path",
    "summary_cache_schema_invalid",
    "summary_cache_storage_unavailable",
    "summary_cache_write_failed",
    "memory_hierarchy_summary_store_error",
})

_EXPECTED_COLUMNS: Final = {
    "summary_meta": (
        "singleton",
        "schema_version",
        "store_contract_version",
        "summary_contract_version",
        "generation",
    ),
    "node_summaries": (
        "node_key",
        "node_type",
        "projection_digest",
        "summary_digest",
        "authority",
        "clause_count",
        "generation",
    ),
    "summary_clauses": (
        "node_key",
        "ordinal",
        "text",
    ),
    "summary_support": (
        "node_key",
        "clause_ordinal",
        "support_ordinal",
        "memory_key",
    ),
}


class MemoryHierarchySummaryStoreError(RuntimeError):
    """Stable, data-free derived-summary cache failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_summary_store_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_summary_store_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchySummaryStoreError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchySummaryStoreError(category)


@dataclass(frozen=True, slots=True, repr=False)
class CachedNodeSummaryV1:
    """Untrusted derived routing text proved current for one hierarchy revision."""

    node_type: str
    node_key: str
    projection_digest: str = field(repr=False)
    summary_digest: str = field(repr=False)
    authority: str
    generation: int
    clauses: tuple[summary.SummaryClauseProposalV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<CachedNodeSummaryV1 "
            f"type={self.node_type!r} clauses={len(self.clauses)} "
            f"generation={self.generation} authority={self.authority!r}>"
        )

    @property
    def text(self) -> str:
        return "\n".join(clause.text for clause in self.clauses)

    @property
    def support_keys(self) -> tuple[str, ...]:
        return tuple(sorted({
            memory_key
            for clause in self.clauses
            for memory_key in clause.atomic_keys
        }))


@dataclass(frozen=True, slots=True, repr=False)
class SummaryCacheWriteResultV1:
    generation: int
    created: bool
    replaced: bool
    replayed: bool

    def __repr__(self) -> str:
        return (
            "<SummaryCacheWriteResultV1 "
            f"generation={self.generation} created={self.created!r} "
            f"replaced={self.replaced!r} replayed={self.replayed!r}>"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SummaryCachePruneResultV1:
    generation: int
    removed_count: int

    def __repr__(self) -> str:
        return (
            "<SummaryCachePruneResultV1 "
            f"generation={self.generation} removed={self.removed_count}>"
        )


def _validated_path(raw_path: object, *, must_exist: bool) -> Path:
    if not isinstance(raw_path, (str, Path)):
        _raise("invalid_summary_store_path")
    try:
        path = Path(raw_path)
        text = str(path)
    except (OSError, TypeError, ValueError):
        _raise("invalid_summary_store_path")
    if (
        not text
        or len(text) > _MAX_PATH_CHARS
        or "\x00" in text
        or path.name in {"", ".", ".."}
    ):
        _raise("invalid_summary_store_path")
    try:
        if not path.parent.is_dir():
            _raise("invalid_summary_store_path")
        if path.exists() and not path.is_file():
            _raise("invalid_summary_store_path")
        if must_exist and not path.is_file():
            _raise("summary_cache_storage_unavailable")
    except MemoryHierarchySummaryStoreError:
        raise
    except OSError:
        _raise("summary_cache_storage_unavailable")
    return path


def _resolved_path(raw_path: object) -> Path:
    path = _validated_path(raw_path, must_exist=False)
    try:
        return path.resolve(strict=False)
    except OSError:
        _raise("invalid_summary_store_path")


def _validate_forbidden_aliases(path: Path, forbidden_paths: object) -> None:
    if forbidden_paths is None:
        return
    if type(forbidden_paths) not in (list, tuple):
        _raise("invalid_summary_store_path")
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        _raise("invalid_summary_store_path")
    for raw_forbidden in forbidden_paths:
        if not isinstance(raw_forbidden, (str, Path)):
            _raise("invalid_summary_store_path")
        try:
            forbidden = Path(raw_forbidden).resolve(strict=False)
        except (OSError, TypeError, ValueError):
            _raise("invalid_summary_store_path")
        if forbidden == resolved:
            _raise("invalid_summary_store_path")


def _connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error:
        _raise("summary_cache_storage_unavailable")


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
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except (OSError, sqlite3.Error):
        _raise("summary_cache_storage_unavailable")


def _user_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        """SELECT name FROM sqlite_master
             WHERE type='table' AND name NOT LIKE 'sqlite_%'
             ORDER BY name"""
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _create_schema(conn: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE summary_meta(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL CHECK(schema_version=1),
            store_contract_version TEXT NOT NULL,
            summary_contract_version TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>=0)
        )""",
        """CREATE TABLE node_summaries(
            node_key TEXT PRIMARY KEY,
            node_type TEXT NOT NULL CHECK(node_type IN ('topic','canonical_state')),
            projection_digest TEXT NOT NULL,
            summary_digest TEXT NOT NULL,
            authority TEXT NOT NULL CHECK(authority='derived_routing_only'),
            clause_count INTEGER NOT NULL CHECK(clause_count>0),
            generation INTEGER NOT NULL CHECK(generation>0)
        )""",
        """CREATE TABLE summary_clauses(
            node_key TEXT NOT NULL REFERENCES node_summaries(node_key)
                ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal>=0),
            text TEXT NOT NULL,
            PRIMARY KEY(node_key,ordinal)
        )""",
        """CREATE TABLE summary_support(
            node_key TEXT NOT NULL,
            clause_ordinal INTEGER NOT NULL,
            support_ordinal INTEGER NOT NULL CHECK(support_ordinal>=0),
            memory_key TEXT NOT NULL,
            PRIMARY KEY(node_key,clause_ordinal,support_ordinal),
            UNIQUE(node_key,clause_ordinal,memory_key),
            FOREIGN KEY(node_key,clause_ordinal)
                REFERENCES summary_clauses(node_key,ordinal) ON DELETE CASCADE
        )""",
        "CREATE INDEX summary_support_memory_idx ON summary_support(memory_key,node_key)",
        "CREATE INDEX node_summaries_revision_idx ON node_summaries(node_type,projection_digest,node_key)",
    )
    for statement in statements:
        conn.execute(statement)
    conn.execute(
        """INSERT INTO summary_meta
           (singleton,schema_version,store_contract_version,
            summary_contract_version,generation)
           VALUES(1,?,?,?,?,?)""".replace("?,?,?,?,?", "?,?,?,?"),
        (
            SUMMARY_STORE_SCHEMA_VERSION,
            SUMMARY_STORE_CONTRACT_VERSION,
            summary.SUMMARY_CONTRACT_VERSION,
            0,
        ),
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(str(row[1]) for row in rows)


def _validate_schema(conn: sqlite3.Connection) -> sqlite3.Row:
    try:
        if _user_tables(conn) != tuple(sorted(_EXPECTED_COLUMNS)):
            _raise("summary_cache_schema_invalid")
        for table, columns in _EXPECTED_COLUMNS.items():
            if _table_columns(conn, table) != columns:
                _raise("summary_cache_schema_invalid")
        rows = conn.execute("SELECT * FROM summary_meta").fetchall()
        if len(rows) != 1:
            _raise("summary_cache_schema_invalid")
        meta = rows[0]
        if (
            meta["singleton"] != 1
            or meta["schema_version"] != SUMMARY_STORE_SCHEMA_VERSION
            or meta["store_contract_version"] != SUMMARY_STORE_CONTRACT_VERSION
            or meta["summary_contract_version"] != summary.SUMMARY_CONTRACT_VERSION
            or type(meta["generation"]) is not int
            or meta["generation"] < 0
        ):
            _raise("summary_cache_schema_invalid")
        fk_support = conn.execute("PRAGMA foreign_key_list(summary_support)").fetchall()
        if len(fk_support) != 2:
            _raise("summary_cache_schema_invalid")
        fk_clauses = conn.execute("PRAGMA foreign_key_list(summary_clauses)").fetchall()
        if len(fk_clauses) != 1 or fk_clauses[0]["table"] != "node_summaries":
            _raise("summary_cache_schema_invalid")
        return meta
    except MemoryHierarchySummaryStoreError:
        raise
    except sqlite3.Error:
        _raise("summary_cache_schema_invalid")


def initialize_summary_store(
    raw_path: object,
    *,
    forbidden_paths: object = (),
) -> Path:
    """Create/validate one cache while rejecting relay/hierarchy path aliases."""

    path = _validated_path(raw_path, must_exist=False)
    _validate_forbidden_aliases(path, forbidden_paths)
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
        except MemoryHierarchySummaryStoreError:
            raise
        except sqlite3.Error:
            _raise("summary_cache_storage_unavailable")
    finally:
        conn.close()
    return path


def _validate_current_node(raw_node: object) -> hierarchy.ProjectionNodePlanV1:
    if type(raw_node) is not hierarchy.ProjectionNodePlanV1:
        _raise("invalid_current_summary_node")
    if (
        raw_node.node_type not in _SUPPORTED_NODE_TYPES
        or type(raw_node.node_key) is not str
        or _NODE_KEY_PATTERN.fullmatch(raw_node.node_key) is None
        or type(raw_node.projection_digest) is not str
        or _DIGEST_PATTERN.fullmatch(raw_node.projection_digest) is None
        or type(raw_node.atomic_keys) is not tuple
        or not raw_node.atomic_keys
        or tuple(sorted(raw_node.atomic_keys)) != raw_node.atomic_keys
        or len(set(raw_node.atomic_keys)) != len(raw_node.atomic_keys)
        or any(
            type(memory_key) is not str
            or _MEMORY_KEY_PATTERN.fullmatch(memory_key) is None
            for memory_key in raw_node.atomic_keys
        )
    ):
        _raise("invalid_current_summary_node")
    return raw_node


def _canonical_clause(raw_clause: object) -> summary.SummaryClauseProposalV1:
    if type(raw_clause) is not summary.SummaryClauseProposalV1:
        _raise("invalid_summary_cache_entry")
    if (
        type(raw_clause.atomic_keys) is not tuple
        or not raw_clause.atomic_keys
        or tuple(sorted(raw_clause.atomic_keys)) != raw_clause.atomic_keys
        or len(set(raw_clause.atomic_keys)) != len(raw_clause.atomic_keys)
        or any(
            type(memory_key) is not str
            or _MEMORY_KEY_PATTERN.fullmatch(memory_key) is None
            for memory_key in raw_clause.atomic_keys
        )
    ):
        _raise("invalid_summary_cache_entry")
    policy = memory_policy.MemoryPolicy(
        max_item_chars=summary.MAX_CLAUSE_CHARS,
        sensitive_storage_enabled=False,
    )
    try:
        normalized = policy.validate_content(raw_clause.text, "normal")
    except memory_policy.MemoryPolicyError:
        _raise("invalid_summary_cache_entry")
    if normalized != raw_clause.text:
        _raise("invalid_summary_cache_entry")
    return summary.SummaryClauseProposalV1(raw_clause.atomic_keys, normalized)


def _recomputed_summary_digest(
    node_type: str,
    node_key: str,
    projection_digest: str,
    clauses: tuple[summary.SummaryClauseProposalV1, ...],
) -> str:
    target = summary.SummaryTargetV1(
        node_type=node_type,
        node_key=node_key,
        projection_digest=projection_digest,
        atomics=(),
        episode_groups=(),
    )
    return summary._summary_digest(target, clauses)


def _validate_summary_for_node(
    raw_summary: object,
    current_node: object,
) -> tuple[
    summary.DerivedNodeSummaryV1,
    hierarchy.ProjectionNodePlanV1,
]:
    node = _validate_current_node(current_node)
    if type(raw_summary) is not summary.DerivedNodeSummaryV1:
        _raise("invalid_summary_cache_entry")
    if (
        raw_summary.contract_version != summary.SUMMARY_CONTRACT_VERSION
        or raw_summary.authority != summary.SUMMARY_AUTHORITY
        or raw_summary.node_type != node.node_type
        or raw_summary.node_key != node.node_key
        or raw_summary.projection_digest != node.projection_digest
        or type(raw_summary.summary_digest) is not str
        or _DIGEST_PATTERN.fullmatch(raw_summary.summary_digest) is None
        or type(raw_summary.clauses) is not tuple
        or not raw_summary.clauses
        or len(raw_summary.clauses) > summary.MAX_SUMMARY_CLAUSES
    ):
        _raise("invalid_summary_cache_entry")
    clauses = tuple(_canonical_clause(item) for item in raw_summary.clauses)
    if clauses != raw_summary.clauses:
        _raise("invalid_summary_cache_entry")
    if sum(len(item.text) for item in clauses) + max(0, len(clauses) - 1) > summary.MAX_TOTAL_SUMMARY_CHARS:
        _raise("invalid_summary_cache_entry")
    support = tuple(sorted({
        memory_key
        for item in clauses
        for memory_key in item.atomic_keys
    }))
    if support != node.atomic_keys:
        _raise("invalid_summary_cache_entry")
    expected_digest = _recomputed_summary_digest(
        node.node_type,
        node.node_key,
        node.projection_digest,
        clauses,
    )
    if expected_digest != raw_summary.summary_digest:
        _raise("invalid_summary_cache_entry")
    return raw_summary, node


def _load_cached_from_connection(
    conn: sqlite3.Connection,
    current_node: hierarchy.ProjectionNodePlanV1,
) -> CachedNodeSummaryV1 | None:
    row = conn.execute(
        "SELECT * FROM node_summaries WHERE node_key=?",
        (current_node.node_key,),
    ).fetchone()
    if row is None:
        return None
    if (
        row["node_type"] != current_node.node_type
        or row["projection_digest"] != current_node.projection_digest
    ):
        return None
    if (
        type(row["summary_digest"]) is not str
        or _DIGEST_PATTERN.fullmatch(row["summary_digest"]) is None
        or row["authority"] != summary.SUMMARY_AUTHORITY
        or type(row["clause_count"]) is not int
        or not 1 <= row["clause_count"] <= summary.MAX_SUMMARY_CLAUSES
        or type(row["generation"]) is not int
        or row["generation"] <= 0
    ):
        _raise("summary_cache_schema_invalid")

    clause_rows = conn.execute(
        """SELECT ordinal,text FROM summary_clauses
            WHERE node_key=? ORDER BY ordinal""",
        (current_node.node_key,),
    ).fetchall()
    ordinals = tuple(int(item["ordinal"]) for item in clause_rows)
    if (
        len(clause_rows) != row["clause_count"]
        or ordinals != tuple(range(len(clause_rows)))
    ):
        _raise("summary_cache_schema_invalid")

    clauses: list[summary.SummaryClauseProposalV1] = []
    for clause_row in clause_rows:
        ordinal = int(clause_row["ordinal"])
        support_rows = conn.execute(
            """SELECT support_ordinal,memory_key FROM summary_support
                WHERE node_key=? AND clause_ordinal=? ORDER BY support_ordinal""",
            (current_node.node_key, ordinal),
        ).fetchall()
        support_ordinals = tuple(int(item["support_ordinal"]) for item in support_rows)
        if (
            not support_rows
            or support_ordinals != tuple(range(len(support_rows)))
        ):
            _raise("summary_cache_schema_invalid")
        keys = tuple(str(item["memory_key"]) for item in support_rows)
        candidate = summary.SummaryClauseProposalV1(keys, str(clause_row["text"]))
        try:
            canonical = _canonical_clause(candidate)
        except MemoryHierarchySummaryStoreError:
            _raise("summary_cache_schema_invalid")
        if canonical != candidate:
            _raise("summary_cache_schema_invalid")
        clauses.append(canonical)

    clause_tuple = tuple(clauses)
    support = tuple(sorted({
        memory_key
        for item in clause_tuple
        for memory_key in item.atomic_keys
    }))
    if support != current_node.atomic_keys:
        _raise("summary_cache_schema_invalid")
    expected = _recomputed_summary_digest(
        current_node.node_type,
        current_node.node_key,
        current_node.projection_digest,
        clause_tuple,
    )
    if expected != row["summary_digest"]:
        _raise("summary_cache_schema_invalid")
    return CachedNodeSummaryV1(
        node_type=current_node.node_type,
        node_key=current_node.node_key,
        projection_digest=current_node.projection_digest,
        summary_digest=row["summary_digest"],
        authority=row["authority"],
        generation=row["generation"],
        clauses=clause_tuple,
    )


def load_current_summary(
    raw_path: object,
    current_node: object,
) -> CachedNodeSummaryV1 | None:
    """Return text only for an exact current node revision; stale rows are hidden."""

    path = _validated_path(raw_path, must_exist=True)
    node = _validate_current_node(current_node)
    conn = _connect_readonly(path)
    try:
        _validate_schema(conn)
        return _load_cached_from_connection(conn, node)
    except MemoryHierarchySummaryStoreError:
        raise
    except sqlite3.Error:
        _raise("summary_cache_storage_unavailable")
    finally:
        conn.close()


def _before_summary_commit(_conn: sqlite3.Connection) -> None:
    """Private test seam immediately before transactional commit."""


def store_summary(
    raw_path: object,
    raw_summary: object,
    current_node: object,
) -> SummaryCacheWriteResultV1:
    """Atomically upsert one exact derived summary revision."""

    path = _validated_path(raw_path, must_exist=True)
    derived, node = _validate_summary_for_node(raw_summary, current_node)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            meta = _validate_schema(conn)
            existing = _load_cached_from_connection(conn, node)
            if existing is not None and existing.summary_digest == derived.summary_digest:
                conn.execute("COMMIT")
                return SummaryCacheWriteResultV1(
                    generation=existing.generation,
                    created=False,
                    replaced=False,
                    replayed=True,
                )
            prior_row = conn.execute(
                "SELECT node_key FROM node_summaries WHERE node_key=?",
                (node.node_key,),
            ).fetchone()
            generation = int(meta["generation"]) + 1
            conn.execute(
                """INSERT INTO node_summaries
                   (node_key,node_type,projection_digest,summary_digest,
                    authority,clause_count,generation)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(node_key) DO UPDATE SET
                     node_type=excluded.node_type,
                     projection_digest=excluded.projection_digest,
                     summary_digest=excluded.summary_digest,
                     authority=excluded.authority,
                     clause_count=excluded.clause_count,
                     generation=excluded.generation""",
                (
                    node.node_key,
                    node.node_type,
                    node.projection_digest,
                    derived.summary_digest,
                    summary.SUMMARY_AUTHORITY,
                    len(derived.clauses),
                    generation,
                ),
            )
            conn.execute("DELETE FROM summary_clauses WHERE node_key=?", (node.node_key,))
            for clause_ordinal, item in enumerate(derived.clauses):
                conn.execute(
                    "INSERT INTO summary_clauses(node_key,ordinal,text) VALUES(?,?,?)",
                    (node.node_key, clause_ordinal, item.text),
                )
                for support_ordinal, memory_key in enumerate(item.atomic_keys):
                    conn.execute(
                        """INSERT INTO summary_support
                           (node_key,clause_ordinal,support_ordinal,memory_key)
                           VALUES(?,?,?,?)""",
                        (
                            node.node_key,
                            clause_ordinal,
                            support_ordinal,
                            memory_key,
                        ),
                    )
            conn.execute(
                "UPDATE summary_meta SET generation=? WHERE singleton=1",
                (generation,),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                _raise("summary_cache_write_failed")
            _load_cached_from_connection(conn, node)
            _before_summary_commit(conn)
            conn.execute("COMMIT")
            return SummaryCacheWriteResultV1(
                generation=generation,
                created=prior_row is None,
                replaced=prior_row is not None,
                replayed=False,
            )
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
    except MemoryHierarchySummaryStoreError:
        raise
    except (OSError, sqlite3.Error):
        _raise("summary_cache_storage_unavailable")
    except Exception:
        _raise("summary_cache_write_failed")
    finally:
        conn.close()


def prune_stale_summaries(
    raw_path: object,
    current_nodes: object,
) -> SummaryCachePruneResultV1:
    """Delete cache rows whose current Topic/State revision no longer exists."""

    if type(current_nodes) not in (list, tuple):
        _raise("invalid_current_summary_node")
    nodes: dict[str, hierarchy.ProjectionNodePlanV1] = {}
    for raw_node in current_nodes:
        node = _validate_current_node(raw_node)
        if node.node_key in nodes:
            _raise("invalid_current_summary_node")
        nodes[node.node_key] = node

    path = _validated_path(raw_path, must_exist=True)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            meta = _validate_schema(conn)
            rows = conn.execute(
                "SELECT node_key,node_type,projection_digest FROM node_summaries"
            ).fetchall()
            stale = [
                row["node_key"]
                for row in rows
                if (
                    row["node_key"] not in nodes
                    or nodes[row["node_key"]].node_type != row["node_type"]
                    or nodes[row["node_key"]].projection_digest
                    != row["projection_digest"]
                )
            ]
            if not stale:
                conn.execute("COMMIT")
                return SummaryCachePruneResultV1(
                    generation=int(meta["generation"]),
                    removed_count=0,
                )
            generation = int(meta["generation"]) + 1
            conn.executemany(
                "DELETE FROM node_summaries WHERE node_key=?",
                ((node_key,) for node_key in stale),
            )
            conn.execute(
                "UPDATE summary_meta SET generation=? WHERE singleton=1",
                (generation,),
            )
            _before_summary_commit(conn)
            conn.execute("COMMIT")
            return SummaryCachePruneResultV1(
                generation=generation,
                removed_count=len(stale),
            )
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
    except MemoryHierarchySummaryStoreError:
        raise
    except (OSError, sqlite3.Error):
        _raise("summary_cache_storage_unavailable")
    except Exception:
        _raise("summary_cache_write_failed")
    finally:
        conn.close()
