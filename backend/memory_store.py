"""Transactional SQLite store for explicit derived memories."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

try:
    from . import channel_store, memory_policy
except ImportError:  # support direct module execution in local tooling
    import channel_store
    import memory_policy


class MemoryStoreError(RuntimeError):
    """A fixed, data-free storage error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class StoreResult:
    outcome: str
    item: dict | None = field(default=None, repr=False)


def _validate_digest(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise MemoryStoreError("invalid_fingerprint")
    return value


def _safe_item(row: sqlite3.Row) -> dict:
    return {
        "memory_key": row["memory_key"],
        "kind": row["kind"],
        "scope_type": row["scope_type"],
        "scope_ref": row["scope_ref"],
        "normalized_content": row["normalized_content"],
        "fingerprint_version": int(row["fingerprint_version"]),
        "status": row["status"],
        "explicitness": row["explicitness"],
        "confidence": float(row["confidence"]),
        "sensitivity": row["sensitivity"],
        "first_observed_at": row["first_observed_at"],
        "last_confirmed_at": row["last_confirmed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class MemoryStore:
    """All Memory Core writes are serialized transactions on the relay database."""

    HARD_MAX_ITEMS = 100

    def __init__(self, path: str):
        self.path = path

    def validate_schema(self) -> bool:
        try:
            with channel_store.connect(self.path) as conn:
                channel_store.validate_memory_schema(conn)
            return True
        except (OSError, sqlite3.Error, ValueError):
            return False

    @staticmethod
    def _derive_evidence_role(direction: object, kind: object) -> str:
        if direction == "in" and kind in {"user", "voice"}:
            return "user"
        if direction == "out" and kind in {"reply", "voice"}:
            return "assistant"
        raise MemoryStoreError("invalid_source")

    @classmethod
    def _validate_sources(
        cls, conn: sqlite3.Connection, sources: Iterable[memory_policy.ProvenanceInput],
    ) -> tuple[memory_policy.ProvenanceInput, ...]:
        unique: dict[tuple[int, str], memory_policy.ProvenanceInput] = {}
        for source in sources:
            key = (source.canonical_message_id, source.evidence_type)
            previous = unique.get(key)
            if previous is not None:
                if previous != source:
                    raise MemoryStoreError("invalid_source")
                continue
            row = conn.execute(
                "SELECT direction,kind,meta FROM messages WHERE id=?",
                (source.canonical_message_id,),
            ).fetchone()
            if row is None:
                raise MemoryStoreError("invalid_source")
            try:
                meta = json.loads(row["meta"])
            except (TypeError, ValueError, json.JSONDecodeError):
                raise MemoryStoreError("invalid_source") from None
            if not isinstance(meta, dict):
                raise MemoryStoreError("invalid_source")
            actual_channel = meta.get("channel")
            actual_source = meta.get("source", "")
            if (
                not isinstance(actual_channel, str)
                or not actual_channel
                or not isinstance(actual_source, str)
            ):
                raise MemoryStoreError("invalid_source")
            actual_role = cls._derive_evidence_role(row["direction"], row["kind"])
            if (
                actual_channel != source.channel
                or actual_source != source.source
                or actual_role != source.evidence_role
            ):
                raise MemoryStoreError("invalid_source")
            unique[key] = source
        if not unique:
            raise MemoryStoreError("invalid_source")
        return tuple(unique.values())

    @staticmethod
    def _find_live_by_fingerprint(
        conn: sqlite3.Connection,
        *,
        scope_type: str,
        scope_ref: str,
        kind: str,
        fingerprint_version: int,
        fingerprint: bytes,
    ) -> sqlite3.Row | None:
        rows = conn.execute(
            """SELECT * FROM memory_items
               WHERE scope_type=? AND scope_ref=? AND kind=? AND fingerprint_version=?
                 AND status IN ('active','candidate')
               ORDER BY id""",
            (scope_type, scope_ref, kind, fingerprint_version),
        ).fetchall()
        matched = None
        for row in rows:
            stored = row["normalized_fingerprint"]
            if isinstance(stored, bytes) and memory_policy.secure_digest_equal(stored, fingerprint):
                matched = row
        return matched

    @staticmethod
    def _is_suppressed_conn(
        conn: sqlite3.Connection,
        *,
        scope_type: str,
        scope_ref: str,
        kind: str,
        fingerprint_version: int,
        fingerprint: bytes,
    ) -> bool:
        rows = conn.execute(
            """SELECT normalized_fingerprint FROM memory_suppressions
               WHERE scope_type=? AND scope_ref=? AND kind=? AND fingerprint_version=?
               ORDER BY id""",
            (scope_type, scope_ref, kind, fingerprint_version),
        ).fetchall()
        matched = False
        for row in rows:
            stored = row["normalized_fingerprint"]
            current = (
                isinstance(stored, bytes)
                and memory_policy.secure_digest_equal(stored, fingerprint)
            )
            matched = current or matched
        return matched

    @staticmethod
    def _insert_sources(
        conn: sqlite3.Connection,
        memory_id: int,
        sources: Sequence[memory_policy.ProvenanceInput],
        stamp: str,
    ) -> None:
        for source in sources:
            conn.execute(
                """INSERT INTO memory_sources
                   (memory_id,canonical_message_id,channel,source,evidence_role,evidence_type,created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(memory_id,canonical_message_id,evidence_type) DO NOTHING""",
                (
                    memory_id, source.canonical_message_id, source.channel, source.source,
                    source.evidence_role, source.evidence_type, stamp,
                ),
            )

    @staticmethod
    def _insert_suppression(
        conn: sqlite3.Connection,
        *,
        scope_type: str,
        scope_ref: str,
        kind: str,
        fingerprint: bytes,
        fingerprint_version: int,
        reason_category: str,
        stamp: str,
    ) -> None:
        conn.execute(
            """INSERT INTO memory_suppressions
               (scope_type,scope_ref,kind,normalized_fingerprint,fingerprint_version,
                reason_category,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(scope_type,scope_ref,kind,fingerprint_version,normalized_fingerprint)
               DO NOTHING""",
            (
                scope_type, scope_ref, kind, fingerprint, fingerprint_version,
                reason_category, stamp,
            ),
        )

    def is_suppressed(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        kind: str,
        fingerprint: bytes,
        fingerprint_version: int,
    ) -> bool:
        _validate_digest(fingerprint)
        try:
            with channel_store.connect(self.path) as conn:
                return self._is_suppressed_conn(
                    conn,
                    scope_type=scope_type,
                    scope_ref=scope_ref,
                    kind=kind,
                    fingerprint=fingerprint,
                    fingerprint_version=fingerprint_version,
                )
        except MemoryStoreError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def create_item_with_sources(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        normalized_content: str,
        fingerprint: bytes,
        fingerprint_version: int,
        sensitivity: str,
        sources: Sequence[memory_policy.ProvenanceInput],
        explicitness: str = "explicit",
        confidence: float = 1.0,
    ) -> StoreResult:
        _validate_digest(fingerprint)
        try:
            with channel_store.connect(self.path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    validated_sources = self._validate_sources(conn, sources)
                    if self._is_suppressed_conn(
                        conn,
                        scope_type=scope_type,
                        scope_ref=scope_ref,
                        kind=kind,
                        fingerprint=fingerprint,
                        fingerprint_version=fingerprint_version,
                    ):
                        conn.execute("COMMIT")
                        return StoreResult("suppressed")
                    existing = self._find_live_by_fingerprint(
                        conn,
                        scope_type=scope_type,
                        scope_ref=scope_ref,
                        kind=kind,
                        fingerprint=fingerprint,
                        fingerprint_version=fingerprint_version,
                    )
                    stamp = channel_store.now_iso()
                    if existing is not None:
                        if existing["normalized_content"] != normalized_content:
                            raise MemoryStoreError("conflict")
                        self._insert_sources(conn, int(existing["id"]), validated_sources, stamp)
                        conn.execute("COMMIT")
                        return StoreResult("idempotent_existing", _safe_item(existing))

                    memory_key = secrets.token_urlsafe(24)
                    cursor = conn.execute(
                        """INSERT INTO memory_items
                           (memory_key,kind,scope_type,scope_ref,normalized_content,
                            normalized_fingerprint,fingerprint_version,status,explicitness,
                            confidence,sensitivity,first_observed_at,last_confirmed_at,
                            superseded_by_id,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,'active',?,?,?,?,?,NULL,?,?)""",
                        (
                            memory_key, kind, scope_type, scope_ref, normalized_content,
                            fingerprint, fingerprint_version, explicitness, confidence, sensitivity,
                            stamp, stamp, stamp, stamp,
                        ),
                    )
                    memory_id = int(cursor.lastrowid)
                    self._insert_sources(conn, memory_id, validated_sources, stamp)
                    row = conn.execute(
                        "SELECT * FROM memory_items WHERE id=?", (memory_id,),
                    ).fetchone()
                    conn.execute("COMMIT")
                    return StoreResult("created", _safe_item(row))
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        except MemoryStoreError:
            raise
        except sqlite3.IntegrityError:
            raise MemoryStoreError("conflict") from None
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def get_item_by_key(self, memory_key: str) -> dict | None:
        try:
            with channel_store.connect(self.path) as conn:
                row = conn.execute(
                    "SELECT * FROM memory_items WHERE memory_key=?", (memory_key,),
                ).fetchone()
            return _safe_item(row) if row is not None else None
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def get_sources(self, memory_key: str) -> list[dict]:
        try:
            with channel_store.connect(self.path) as conn:
                rows = conn.execute(
                    """SELECT s.channel,s.source,s.evidence_role,s.evidence_type,s.created_at
                       FROM memory_sources s JOIN memory_items m ON m.id=s.memory_id
                       WHERE m.memory_key=? ORDER BY s.id""",
                    (memory_key,),
                ).fetchall()
            return [
                {
                    "channel": row["channel"],
                    "source": row["source"],
                    "evidence_role": row["evidence_role"],
                    "evidence_type": row["evidence_type"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def get_active_items(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        kinds: Sequence[str] | None = None,
        sensitivities: Sequence[str] = ("normal",),
        limit: int = 20,
    ) -> list[dict]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise MemoryStoreError("invalid_query")
        limit = min(limit, self.HARD_MAX_ITEMS)
        kinds = tuple(kinds or ())
        sensitivities = tuple(sensitivities)
        if not sensitivities:
            return []
        if any(item not in memory_policy.KINDS for item in kinds):
            raise MemoryStoreError("invalid_query")
        if any(item not in memory_policy.SENSITIVITIES for item in sensitivities):
            raise MemoryStoreError("invalid_query")
        clauses = ["status='active'", "scope_type=?", "scope_ref=?"]
        parameters: list[object] = [scope_type, scope_ref]
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
            parameters.extend(kinds)
        clauses.append("sensitivity IN (%s)" % ",".join("?" for _ in sensitivities))
        parameters.extend(sensitivities)
        parameters.append(limit)
        sql = (
            "SELECT * FROM memory_items WHERE " + " AND ".join(clauses)
            + " ORDER BY last_confirmed_at DESC,id DESC LIMIT ?"
        )
        try:
            with channel_store.connect(self.path) as conn:
                rows = conn.execute(sql, tuple(parameters)).fetchall()
            return [_safe_item(row) for row in rows]
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def correct_item_atomic(
        self,
        *,
        memory_key: str,
        normalized_content: str,
        fingerprint: bytes,
        fingerprint_version: int,
        sensitivity: str,
        sources: Sequence[memory_policy.ProvenanceInput],
    ) -> StoreResult:
        _validate_digest(fingerprint)
        try:
            with channel_store.connect(self.path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    old = conn.execute(
                        "SELECT * FROM memory_items WHERE memory_key=?", (memory_key,),
                    ).fetchone()
                    if old is None:
                        raise MemoryStoreError("not_found")
                    if old["status"] != "active":
                        raise MemoryStoreError("invalid_state")
                    validated_sources = self._validate_sources(conn, sources)
                    old_fingerprint = old["normalized_fingerprint"]
                    if not isinstance(old_fingerprint, bytes):
                        raise MemoryStoreError("invalid_state")
                    stamp = channel_store.now_iso()
                    if memory_policy.secure_digest_equal(old_fingerprint, fingerprint):
                        if old["normalized_content"] != normalized_content:
                            raise MemoryStoreError("conflict")
                        self._insert_sources(conn, int(old["id"]), validated_sources, stamp)
                        conn.execute("COMMIT")
                        return StoreResult("idempotent_noop", _safe_item(old))
                    if self._is_suppressed_conn(
                        conn,
                        scope_type=old["scope_type"],
                        scope_ref=old["scope_ref"],
                        kind=old["kind"],
                        fingerprint=fingerprint,
                        fingerprint_version=fingerprint_version,
                    ):
                        conn.execute("COMMIT")
                        return StoreResult("suppressed")
                    if self._find_live_by_fingerprint(
                        conn,
                        scope_type=old["scope_type"],
                        scope_ref=old["scope_ref"],
                        kind=old["kind"],
                        fingerprint=fingerprint,
                        fingerprint_version=fingerprint_version,
                    ) is not None:
                        raise MemoryStoreError("conflict")

                    new_key = secrets.token_urlsafe(24)
                    cursor = conn.execute(
                        """INSERT INTO memory_items
                           (memory_key,kind,scope_type,scope_ref,normalized_content,
                            normalized_fingerprint,fingerprint_version,status,explicitness,
                            confidence,sensitivity,first_observed_at,last_confirmed_at,
                            superseded_by_id,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,'active','explicit',1.0,?,?,?,NULL,?,?)""",
                        (
                            new_key, old["kind"], old["scope_type"], old["scope_ref"],
                            normalized_content, fingerprint, fingerprint_version, sensitivity,
                            stamp, stamp, stamp, stamp,
                        ),
                    )
                    new_id = int(cursor.lastrowid)
                    self._insert_sources(conn, new_id, validated_sources, stamp)
                    updated = conn.execute(
                        """UPDATE memory_items
                           SET status='superseded',superseded_by_id=?,updated_at=?
                           WHERE id=? AND status='active' AND superseded_by_id IS NULL""",
                        (new_id, stamp, int(old["id"])),
                    )
                    if updated.rowcount != 1:
                        raise MemoryStoreError("conflict")
                    self._insert_suppression(
                        conn,
                        scope_type=old["scope_type"],
                        scope_ref=old["scope_ref"],
                        kind=old["kind"],
                        fingerprint=old_fingerprint,
                        fingerprint_version=int(old["fingerprint_version"]),
                        reason_category="corrected_obsolete",
                        stamp=stamp,
                    )
                    row = conn.execute(
                        "SELECT * FROM memory_items WHERE id=?", (new_id,),
                    ).fetchone()
                    conn.execute("COMMIT")
                    return StoreResult("corrected", _safe_item(row))
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        except MemoryStoreError:
            raise
        except sqlite3.IntegrityError:
            raise MemoryStoreError("conflict") from None
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def forget_item_atomic(self, *, memory_key: str) -> StoreResult:
        try:
            with channel_store.connect(self.path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM memory_items WHERE memory_key=?", (memory_key,),
                    ).fetchone()
                    if row is None:
                        raise MemoryStoreError("not_found")
                    if row["status"] == "forgotten":
                        conn.execute("COMMIT")
                        return StoreResult("already_forgotten", _safe_item(row))
                    if row["status"] != "active":
                        raise MemoryStoreError("invalid_state")
                    fingerprint = row["normalized_fingerprint"]
                    if not isinstance(fingerprint, bytes):
                        raise MemoryStoreError("invalid_state")
                    stamp = channel_store.now_iso()
                    self._insert_suppression(
                        conn,
                        scope_type=row["scope_type"],
                        scope_ref=row["scope_ref"],
                        kind=row["kind"],
                        fingerprint=fingerprint,
                        fingerprint_version=int(row["fingerprint_version"]),
                        reason_category="user_forget",
                        stamp=stamp,
                    )
                    updated = conn.execute(
                        """UPDATE memory_items
                           SET status='forgotten',normalized_content=NULL,
                               normalized_fingerprint=NULL,superseded_by_id=NULL,updated_at=?
                           WHERE id=? AND status='active'""",
                        (stamp, int(row["id"])),
                    )
                    if updated.rowcount != 1:
                        raise MemoryStoreError("conflict")
                    forgotten = conn.execute(
                        "SELECT * FROM memory_items WHERE id=?", (int(row["id"]),),
                    ).fetchone()
                    conn.execute("COMMIT")
                    return StoreResult("forgotten", _safe_item(forgotten))
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        except MemoryStoreError:
            raise
        except sqlite3.IntegrityError:
            raise MemoryStoreError("conflict") from None
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None
