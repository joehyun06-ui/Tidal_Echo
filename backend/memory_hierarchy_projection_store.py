"""Disposable SQLite materialization for Phase 4D-B hierarchy manifests.

The sidecar is a rebuildable projection only. It stores node/member references,
digests, dirty state, and a generation watermark; it never stores Atomic Memory
content, provenance text, suppression authority, approval state, or any other
Memory truth. The authoritative relay database is neither opened nor modified
by this module.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from backend import memory_hierarchy_projection as hierarchy


SIDECAR_SCHEMA_VERSION: Final = 1
SIDECAR_CONTRACT_VERSION: Final = "memory-hierarchy-sidecar-v1"
_MAX_PATH_CHARS: Final = 4096
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_MEMORY_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_NODE_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_NODE_TYPES: Final = frozenset({"topic", "episode", "canonical_state"})

_ERROR_CATEGORIES: Final = frozenset({
    "invalid_projection_plan",
    "invalid_projection_store_path",
    "projection_schema_invalid",
    "projection_storage_unavailable",
    "projection_write_failed",
    "memory_hierarchy_projection_store_error",
})

_EXPECTED_COLUMNS: Final = {
    "projection_meta": (
        "singleton",
        "schema_version",
        "contract_version",
        "projection_contract_version",
        "generation",
        "atomic_snapshot_digest",
    ),
    "projection_nodes": (
        "node_key",
        "node_type",
        "parent_key",
        "projection_digest",
        "dirty",
        "generation",
    ),
    "projection_members": (
        "node_key",
        "ordinal",
        "memory_key",
    ),
}


class MemoryHierarchyProjectionStoreError(RuntimeError):
    """Stable, data-free sidecar failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_hierarchy_projection_store_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "memory_hierarchy_projection_store_error"

    def __repr__(self) -> str:
        return f"MemoryHierarchyProjectionStoreError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchyProjectionStoreError(category)


@dataclass(frozen=True, slots=True, repr=False)
class StoredProjectionNodeV1:
    node_type: str
    node_key: str
    parent_key: str
    projection_digest: str = field(repr=False)
    dirty: bool
    atomic_keys: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<StoredProjectionNodeV1 "
            f"type={self.node_type!r} key={self.node_key!r} dirty={self.dirty!r}>"
        )

    def receipt(self) -> hierarchy.ProjectionNodeReceiptV1:
        return hierarchy.ProjectionNodeReceiptV1(
            node_type=self.node_type,
            node_key=self.node_key,
            parent_key=self.parent_key,
            projection_digest=self.projection_digest,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionStoreSnapshotV1:
    schema_version: int
    contract_version: str
    projection_contract_version: str
    generation: int
    atomic_snapshot_digest: str = field(repr=False)
    nodes: tuple[StoredProjectionNodeV1, ...] = field(repr=False)

    def __repr__(self) -> str:
        return (
            "<ProjectionStoreSnapshotV1 "
            f"generation={self.generation} nodes={len(self.nodes)}>"
        )

    @property
    def dirty_node_keys(self) -> tuple[str, ...]:
        return tuple(node.node_key for node in self.nodes if node.dirty)

    @property
    def member_count(self) -> int:
        return sum(len(node.atomic_keys) for node in self.nodes)

    def receipts(self) -> tuple[hierarchy.ProjectionNodeReceiptV1, ...]:
        return tuple(node.receipt() for node in self.nodes)


def _validated_path(raw_path: object, *, must_exist: bool) -> Path:
    if not isinstance(raw_path, (str, Path)):
        _raise("invalid_projection_store_path")
    try:
        path = Path(raw_path)
        text = str(path)
    except (TypeError, ValueError, OSError):
        _raise("invalid_projection_store_path")
    if (
        not text
        or len(text) > _MAX_PATH_CHARS
        or "\x00" in text
        or path.name in {"", ".", ".."}
    ):
        _raise("invalid_projection_store_path")
    try:
        parent = path.parent
        if not parent.is_dir():
            _raise("invalid_projection_store_path")
        if path.exists() and not path.is_file():
            _raise("invalid_projection_store_path")
        if must_exist and not path.is_file():
            _raise("projection_storage_unavailable")
    except MemoryHierarchyProjectionStoreError:
        raise
    except OSError:
        _raise("projection_storage_unavailable")
    return path


def _connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except sqlite3.Error:
        _raise("projection_storage_unavailable")


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
        _raise("projection_storage_unavailable")


def _user_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        """SELECT name FROM sqlite_master
             WHERE type='table' AND name NOT LIKE 'sqlite_%'
             ORDER BY name"""
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE projection_meta(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL CHECK(schema_version=1),
            contract_version TEXT NOT NULL,
            projection_contract_version TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK(generation>=0),
            atomic_snapshot_digest TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE projection_nodes(
            node_key TEXT PRIMARY KEY,
            node_type TEXT NOT NULL CHECK(
                node_type IN ('topic','episode','canonical_state')
            ),
            parent_key TEXT NOT NULL,
            projection_digest TEXT NOT NULL,
            dirty INTEGER NOT NULL CHECK(dirty IN (0,1)),
            generation INTEGER NOT NULL CHECK(generation>0)
        )"""
    )
    conn.execute(
        """CREATE TABLE projection_members(
            node_key TEXT NOT NULL REFERENCES projection_nodes(node_key)
                ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal>=0),
            memory_key TEXT NOT NULL,
            PRIMARY KEY(node_key, ordinal),
            UNIQUE(node_key, memory_key)
        )"""
    )
    conn.execute(
        """CREATE INDEX projection_nodes_parent_idx
            ON projection_nodes(parent_key, node_type, node_key)"""
    )
    conn.execute(
        """CREATE INDEX projection_members_memory_idx
            ON projection_members(memory_key, node_key)"""
    )
    conn.execute(
        """INSERT INTO projection_meta
           (singleton,schema_version,contract_version,
            projection_contract_version,generation,atomic_snapshot_digest)
           VALUES(1,?,?,?,?,?)""",
        (
            SIDECAR_SCHEMA_VERSION,
            SIDECAR_CONTRACT_VERSION,
            hierarchy.PROJECTION_CONTRACT_VERSION,
            0,
            "",
        ),
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return tuple(str(row[1]) for row in rows)


def _validate_schema(conn: sqlite3.Connection) -> sqlite3.Row:
    try:
        if _user_tables(conn) != tuple(sorted(_EXPECTED_COLUMNS)):
            _raise("projection_schema_invalid")
        for table, columns in _EXPECTED_COLUMNS.items():
            if _table_columns(conn, table) != columns:
                _raise("projection_schema_invalid")
        fk_rows = conn.execute("PRAGMA foreign_key_list(projection_members)").fetchall()
        if len(fk_rows) != 1 or fk_rows[0]["table"] != "projection_nodes":
            _raise("projection_schema_invalid")
        rows = conn.execute("SELECT * FROM projection_meta").fetchall()
        if len(rows) != 1:
            _raise("projection_schema_invalid")
        meta = rows[0]
        generation = meta["generation"]
        digest = meta["atomic_snapshot_digest"]
        if (
            meta["singleton"] != 1
            or meta["schema_version"] != SIDECAR_SCHEMA_VERSION
            or meta["contract_version"] != SIDECAR_CONTRACT_VERSION
            or meta["projection_contract_version"]
            != hierarchy.PROJECTION_CONTRACT_VERSION
            or type(generation) is not int
            or generation < 0
            or type(digest) is not str
            or (generation == 0 and digest != "")
            or (generation > 0 and _DIGEST_PATTERN.fullmatch(digest) is None)
        ):
            _raise("projection_schema_invalid")
        return meta
    except MemoryHierarchyProjectionStoreError:
        raise
    except sqlite3.Error:
        _raise("projection_schema_invalid")


def initialize_projection_store(raw_path: object) -> Path:
    """Create a new empty sidecar, or validate an existing one without repair."""

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
        except MemoryHierarchyProjectionStoreError:
            raise
        except sqlite3.Error:
            _raise("projection_storage_unavailable")
    finally:
        conn.close()
    return path


def _validate_plan(raw_plan: object) -> hierarchy.HierarchyProjectionPlanV1:
    if type(raw_plan) is not hierarchy.HierarchyProjectionPlanV1:
        _raise("invalid_projection_plan")
    if (
        raw_plan.contract_version != hierarchy.PROJECTION_CONTRACT_VERSION
        or type(raw_plan.atomic_snapshot_digest) is not str
        or _DIGEST_PATTERN.fullmatch(raw_plan.atomic_snapshot_digest) is None
        or type(raw_plan.nodes) is not tuple
        or type(raw_plan.obsolete_node_keys) is not tuple
    ):
        _raise("invalid_projection_plan")

    nodes_by_key: dict[str, hierarchy.ProjectionNodePlanV1] = {}
    states_by_parent: dict[str, int] = {}
    for node in raw_plan.nodes:
        if type(node) is not hierarchy.ProjectionNodePlanV1:
            _raise("invalid_projection_plan")
        if (
            node.node_type not in _NODE_TYPES
            or type(node.node_key) is not str
            or _NODE_KEY_PATTERN.fullmatch(node.node_key) is None
            or type(node.parent_key) is not str
            or (node.parent_key and _NODE_KEY_PATTERN.fullmatch(node.parent_key) is None)
            or type(node.atomic_keys) is not tuple
            or type(node.projection_digest) is not str
            or _DIGEST_PATTERN.fullmatch(node.projection_digest) is None
            or type(node.dirty) is not bool
            or node.node_key in nodes_by_key
        ):
            _raise("invalid_projection_plan")
        if len(set(node.atomic_keys)) != len(node.atomic_keys):
            _raise("invalid_projection_plan")
        for memory_key in node.atomic_keys:
            if type(memory_key) is not str or _MEMORY_KEY_PATTERN.fullmatch(memory_key) is None:
                _raise("invalid_projection_plan")
        if tuple(sorted(node.atomic_keys)) != node.atomic_keys:
            _raise("invalid_projection_plan")
        nodes_by_key[node.node_key] = node
        if node.node_type == "topic":
            if node.parent_key:
                _raise("invalid_projection_plan")
        elif not node.parent_key:
            _raise("invalid_projection_plan")
        if node.node_type == "canonical_state":
            states_by_parent[node.parent_key] = states_by_parent.get(node.parent_key, 0) + 1

    for node in raw_plan.nodes:
        if node.node_type == "topic":
            if states_by_parent.get(node.node_key, 0) != 1:
                _raise("invalid_projection_plan")
            continue
        parent = nodes_by_key.get(node.parent_key)
        if parent is None or parent.node_type != "topic":
            _raise("invalid_projection_plan")
        if not set(node.atomic_keys).issubset(parent.atomic_keys):
            _raise("invalid_projection_plan")
        if node.node_type == "canonical_state" and node.atomic_keys != parent.atomic_keys:
            _raise("invalid_projection_plan")

    obsolete = raw_plan.obsolete_node_keys
    if len(set(obsolete)) != len(obsolete):
        _raise("invalid_projection_plan")
    for node_key in obsolete:
        if (
            type(node_key) is not str
            or _NODE_KEY_PATTERN.fullmatch(node_key) is None
            or node_key in nodes_by_key
        ):
            _raise("invalid_projection_plan")
    return raw_plan


def _write_node_members(
    conn: sqlite3.Connection,
    node: hierarchy.ProjectionNodePlanV1,
    generation: int,
) -> None:
    conn.execute(
        """INSERT INTO projection_nodes
           (node_key,node_type,parent_key,projection_digest,dirty,generation)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(node_key) DO UPDATE SET
               node_type=excluded.node_type,
               parent_key=excluded.parent_key,
               projection_digest=excluded.projection_digest,
               dirty=excluded.dirty,
               generation=excluded.generation""",
        (
            node.node_key,
            node.node_type,
            node.parent_key,
            node.projection_digest,
            1 if node.dirty else 0,
            generation,
        ),
    )
    conn.execute("DELETE FROM projection_members WHERE node_key=?", (node.node_key,))
    conn.executemany(
        """INSERT INTO projection_members(node_key,ordinal,memory_key)
           VALUES(?,?,?)""",
        (
            (node.node_key, ordinal, memory_key)
            for ordinal, memory_key in enumerate(node.atomic_keys)
        ),
    )


def apply_projection_plan(
    raw_path: object,
    raw_plan: object,
) -> ProjectionStoreSnapshotV1:
    """Atomically materialize one complete hierarchy manifest in the sidecar."""

    path = _validated_path(raw_path, must_exist=True)
    plan = _validate_plan(raw_plan)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            meta = _validate_schema(conn)
            generation = int(meta["generation"]) + 1
            current_keys = tuple(node.node_key for node in plan.nodes)
            if current_keys:
                placeholders = ",".join("?" for _ in current_keys)
                conn.execute(
                    f"DELETE FROM projection_nodes WHERE node_key NOT IN ({placeholders})",
                    current_keys,
                )
            else:
                conn.execute("DELETE FROM projection_nodes")
            for node in plan.nodes:
                _write_node_members(conn, node, generation)
            conn.execute(
                """UPDATE projection_meta
                      SET generation=?,atomic_snapshot_digest=?
                    WHERE singleton=1""",
                (generation, plan.atomic_snapshot_digest),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                _raise("projection_write_failed")
            _validate_schema(conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
    except MemoryHierarchyProjectionStoreError:
        raise
    except (OSError, sqlite3.Error):
        _raise("projection_storage_unavailable")
    except Exception:
        _raise("projection_write_failed")
    finally:
        conn.close()
    return load_projection_snapshot(path)


def load_projection_snapshot(raw_path: object) -> ProjectionStoreSnapshotV1:
    """Read one content-free immutable snapshot from the sidecar."""

    path = _validated_path(raw_path, must_exist=True)
    conn = _connect_readonly(path)
    try:
        meta = _validate_schema(conn)
        node_rows = conn.execute(
            """SELECT node_key,node_type,parent_key,projection_digest,dirty
                 FROM projection_nodes
                ORDER BY parent_key,node_type,node_key"""
        ).fetchall()
        nodes: list[StoredProjectionNodeV1] = []
        for row in node_rows:
            member_rows = conn.execute(
                """SELECT ordinal,memory_key FROM projection_members
                    WHERE node_key=? ORDER BY ordinal""",
                (row["node_key"],),
            ).fetchall()
            ordinals = tuple(int(member["ordinal"]) for member in member_rows)
            if ordinals != tuple(range(len(member_rows))):
                _raise("projection_schema_invalid")
            atomic_keys = tuple(str(member["memory_key"]) for member in member_rows)
            if (
                row["node_type"] not in _NODE_TYPES
                or _NODE_KEY_PATTERN.fullmatch(str(row["node_key"])) is None
                or (row["parent_key"] and _NODE_KEY_PATTERN.fullmatch(str(row["parent_key"])) is None)
                or _DIGEST_PATTERN.fullmatch(str(row["projection_digest"])) is None
                or row["dirty"] not in (0, 1)
                or len(set(atomic_keys)) != len(atomic_keys)
                or any(_MEMORY_KEY_PATTERN.fullmatch(key) is None for key in atomic_keys)
            ):
                _raise("projection_schema_invalid")
            nodes.append(StoredProjectionNodeV1(
                node_type=str(row["node_type"]),
                node_key=str(row["node_key"]),
                parent_key=str(row["parent_key"]),
                projection_digest=str(row["projection_digest"]),
                dirty=bool(row["dirty"]),
                atomic_keys=atomic_keys,
            ))
        return ProjectionStoreSnapshotV1(
            schema_version=int(meta["schema_version"]),
            contract_version=str(meta["contract_version"]),
            projection_contract_version=str(meta["projection_contract_version"]),
            generation=int(meta["generation"]),
            atomic_snapshot_digest=str(meta["atomic_snapshot_digest"]),
            nodes=tuple(nodes),
        )
    except MemoryHierarchyProjectionStoreError:
        raise
    except sqlite3.Error:
        _raise("projection_storage_unavailable")
    finally:
        conn.close()


def load_projection_receipts(
    raw_path: object,
) -> tuple[hierarchy.ProjectionNodeReceiptV1, ...]:
    return load_projection_snapshot(raw_path).receipts()
