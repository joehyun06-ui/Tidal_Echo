"""Transactional SQLite store for explicit derived memories."""

from __future__ import annotations

import json
import hmac
import re
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


@dataclass(frozen=True, repr=False)
class _ValidatedSource:
    canonical_message_id: int
    evidence_event_id: int
    channel: str
    source: str
    evidence_role: str
    evidence_type: str


MAX_CANONICAL_META_BYTES = 16 * 1024
MAX_CANONICAL_META_KEYS = 64
MAX_CANONICAL_META_DEPTH = 8
MAX_CANONICAL_META_STRING_CHARS = 4096
_SAFE_META_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_SENSITIVITY_RANK = {"normal": 0, "sensitive": 1, "restricted": 2}


def _validate_digest(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) != 32:
        raise MemoryStoreError("invalid_fingerprint")
    return value


def _load_bounded_meta(raw: object) -> dict:
    if not isinstance(raw, str):
        raise MemoryStoreError("invalid_provenance")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError:
        raise MemoryStoreError("invalid_provenance") from None
    if len(encoded) > MAX_CANONICAL_META_BYTES:
        raise MemoryStoreError("invalid_provenance")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise MemoryStoreError("invalid_provenance") from None
    if not isinstance(payload, dict):
        raise MemoryStoreError("invalid_provenance")
    key_count = 0
    stack: list[tuple[object, int]] = [(payload, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_CANONICAL_META_DEPTH:
            raise MemoryStoreError("invalid_provenance")
        if isinstance(value, dict):
            key_count += len(value)
            if key_count > MAX_CANONICAL_META_KEYS:
                raise MemoryStoreError("invalid_provenance")
            for key, nested in value.items():
                if (
                    not isinstance(key, str)
                    or len(key) > MAX_CANONICAL_META_STRING_CHARS
                ):
                    raise MemoryStoreError("invalid_provenance")
                stack.append((nested, depth + 1))
        elif isinstance(value, list):
            stack.extend((nested, depth + 1) for nested in value)
        elif isinstance(value, str):
            if len(value) > MAX_CANONICAL_META_STRING_CHARS:
                raise MemoryStoreError("invalid_provenance")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise MemoryStoreError("invalid_provenance")
    return payload


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
        raise MemoryStoreError("invalid_provenance")

    @classmethod
    def _validate_sources(
        cls,
        conn: sqlite3.Connection,
        sources: Iterable[memory_policy.ProvenanceInput],
        *,
        memory_kind: str,
        operation: str,
    ) -> tuple[_ValidatedSource, ...]:
        unique: dict[int, _ValidatedSource] = {}
        for source in sources:
            if source.canonical_message_id in unique:
                continue
            row = conn.execute(
                """SELECT m.direction,m.kind,m.meta,
                          e.id AS evidence_event_id,e.evidence_type,
                          e.reality_scope,e.subject_scope,e.created_by_component
                   FROM messages m
                   LEFT JOIN memory_evidence_events e
                     ON e.canonical_message_id=m.id
                   WHERE m.id=?""",
                (source.canonical_message_id,),
            ).fetchone()
            if row is None:
                raise MemoryStoreError("invalid_provenance")
            meta = _load_bounded_meta(row["meta"])
            actual_channel = meta.get("channel")
            actual_source = meta.get("source", "")
            if (
                not isinstance(actual_channel, str)
                or not actual_channel
                or not isinstance(actual_source, str)
                or _SAFE_META_VALUE.fullmatch(actual_channel) is None
                or _SAFE_META_VALUE.fullmatch(actual_source) is None
                or actual_channel not in memory_policy.KNOWN_CHANNELS
            ):
                raise MemoryStoreError("invalid_provenance")
            actual_role = cls._derive_evidence_role(row["direction"], row["kind"])
            evidence_event_id = row["evidence_event_id"]
            evidence_type = row["evidence_type"]
            reality_scope = row["reality_scope"]
            subject_scope = row["subject_scope"]
            component = row["created_by_component"]
            if (
                not isinstance(evidence_event_id, int)
                or evidence_type not in memory_policy.ALL_EVIDENCE_TYPES
                or reality_scope not in memory_policy.REALITY_SCOPES
                or subject_scope not in memory_policy.SUBJECT_SCOPES
                or component not in memory_policy.EVIDENCE_COMPONENTS
            ):
                raise MemoryStoreError("unsupported_evidence")
            if reality_scope != "real":
                raise MemoryStoreError("unsupported_evidence")
            if operation == "correct":
                semantic_allowed = (
                    actual_role == "user"
                    and evidence_type in memory_policy.CORRECTION_EVIDENCE_TYPES
                    and subject_scope in {"user", "project"}
                )
            elif memory_kind == "assistant_experience":
                semantic_allowed = (
                    actual_role == "assistant"
                    and evidence_type in memory_policy.ASSISTANT_EVIDENCE_TYPES
                    and subject_scope == "assistant"
                    and component == "assistant_runtime"
                )
            else:
                expected_subject = (
                    "project"
                    if evidence_type == "confirmed_project_decision"
                    else "user"
                )
                semantic_allowed = (
                    actual_role == "user"
                    and evidence_type in memory_policy.USER_EVIDENCE_TYPES
                    and subject_scope == expected_subject
                )
            if not semantic_allowed:
                raise MemoryStoreError("unsupported_evidence")
            unique[source.canonical_message_id] = _ValidatedSource(
                canonical_message_id=source.canonical_message_id,
                evidence_event_id=evidence_event_id,
                channel=actual_channel,
                source=actual_source,
                evidence_role=actual_role,
                evidence_type=evidence_type,
            )
        if not unique:
            raise MemoryStoreError("invalid_provenance")
        return tuple(unique.values())

    @staticmethod
    def _profile_matches(
        row: sqlite3.Row,
        *,
        key_id: str,
        key_check: bytes,
        normalization_version: int,
        fingerprint_version: int,
    ) -> bool:
        stored_key_id = row["key_id"]
        stored_check = row["key_check"]
        return (
            isinstance(stored_key_id, str)
            and hmac.compare_digest(stored_key_id, key_id)
            and memory_policy.secure_digest_equal(stored_check, key_check)
            and row["normalization_version"] == normalization_version
            and row["fingerprint_version"] == fingerprint_version
        )

    def ensure_fingerprint_profile(
        self,
        *,
        key_id: str,
        key_check: bytes,
        normalization_version: int,
        fingerprint_version: int,
    ) -> None:
        _validate_digest(key_check)
        try:
            with channel_store.connect(self.path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT * FROM memory_fingerprint_profile WHERE singleton=1"
                    ).fetchone()
                    if row is None:
                        derived_rows = sum(
                            int(conn.execute(
                                f"SELECT count(*) FROM {table}"
                            ).fetchone()[0])
                            for table in ("memory_items", "memory_suppressions")
                        )
                        if derived_rows:
                            raise MemoryStoreError(
                                "memory_fingerprint_profile_mismatch"
                            )
                        stamp = channel_store.now_iso()
                        conn.execute(
                            """INSERT INTO memory_fingerprint_profile
                               (singleton,key_id,key_check,normalization_version,
                                fingerprint_version,created_at,updated_at)
                               VALUES(1,?,?,?,?,?,?)""",
                            (
                                key_id,
                                key_check,
                                normalization_version,
                                fingerprint_version,
                                stamp,
                                stamp,
                            ),
                        )
                    elif not self._profile_matches(
                        row,
                        key_id=key_id,
                        key_check=key_check,
                        normalization_version=normalization_version,
                        fingerprint_version=fingerprint_version,
                    ):
                        raise MemoryStoreError(
                            "memory_fingerprint_profile_mismatch"
                        )
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
        except MemoryStoreError:
            raise
        except sqlite3.IntegrityError:
            raise MemoryStoreError("memory_fingerprint_profile_mismatch") from None
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def validate_fingerprint_profile(
        self,
        *,
        key_id: str,
        key_check: bytes,
        normalization_version: int,
        fingerprint_version: int,
    ) -> bool:
        _validate_digest(key_check)
        try:
            with channel_store.connect(self.path) as conn:
                row = conn.execute(
                    "SELECT * FROM memory_fingerprint_profile WHERE singleton=1"
                ).fetchone()
            return (
                row is not None
                and self._profile_matches(
                    row,
                    key_id=key_id,
                    key_check=key_check,
                    normalization_version=normalization_version,
                    fingerprint_version=fingerprint_version,
                )
            )
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

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
        sources: Sequence[_ValidatedSource],
        stamp: str,
    ) -> None:
        for source in sources:
            conn.execute(
                """INSERT INTO memory_sources
                   (memory_id,evidence_event_id,canonical_message_id,channel,source,
                    evidence_role,evidence_type,created_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(memory_id,evidence_event_id) DO NOTHING""",
                (
                    memory_id,
                    source.evidence_event_id,
                    source.canonical_message_id,
                    source.channel,
                    source.source,
                    source.evidence_role,
                    source.evidence_type,
                    stamp,
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
        sensitive_storage_enabled: bool,
        explicitness: str = "explicit",
        confidence: float = 1.0,
    ) -> StoreResult:
        _validate_digest(fingerprint)
        try:
            with channel_store.connect(self.path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    validated_sources = self._validate_sources(
                        conn,
                        sources,
                        memory_kind=kind,
                        operation="create",
                    )
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
                        existing_rank = _SENSITIVITY_RANK.get(existing["sensitivity"])
                        requested_rank = _SENSITIVITY_RANK.get(sensitivity)
                        if existing_rank is None or requested_rank is None:
                            raise MemoryStoreError("invalid_state")
                        if requested_rank > existing_rank:
                            conn.execute(
                                """UPDATE memory_items
                                   SET sensitivity=?,last_confirmed_at=?,updated_at=?
                                   WHERE id=?""",
                                (sensitivity, stamp, stamp, int(existing["id"])),
                            )
                        self._insert_sources(conn, int(existing["id"]), validated_sources, stamp)
                        existing = conn.execute(
                            "SELECT * FROM memory_items WHERE id=?",
                            (int(existing["id"]),),
                        ).fetchone()
                        conn.execute("COMMIT")
                        return StoreResult("idempotent_existing", _safe_item(existing))

                    if sensitivity != "normal" and not sensitive_storage_enabled:
                        raise MemoryStoreError("sensitive_storage_disabled")
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
        sensitive_storage_enabled: bool,
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
                    validated_sources = self._validate_sources(
                        conn,
                        sources,
                        memory_kind=old["kind"],
                        operation="correct",
                    )
                    old_fingerprint = old["normalized_fingerprint"]
                    if not isinstance(old_fingerprint, bytes):
                        raise MemoryStoreError("invalid_state")
                    existing_rank = _SENSITIVITY_RANK.get(old["sensitivity"])
                    requested_rank = _SENSITIVITY_RANK.get(sensitivity)
                    if existing_rank is None or requested_rank is None:
                        raise MemoryStoreError("invalid_state")
                    if requested_rank < existing_rank:
                        raise MemoryStoreError("sensitivity_downgrade")
                    stamp = channel_store.now_iso()
                    if memory_policy.secure_digest_equal(old_fingerprint, fingerprint):
                        if old["normalized_content"] != normalized_content:
                            raise MemoryStoreError("conflict")
                        if requested_rank > existing_rank:
                            conn.execute(
                                """UPDATE memory_items
                                   SET sensitivity=?,last_confirmed_at=?,updated_at=?
                                   WHERE id=?""",
                                (sensitivity, stamp, stamp, int(old["id"])),
                            )
                        self._insert_sources(conn, int(old["id"]), validated_sources, stamp)
                        old = conn.execute(
                            "SELECT * FROM memory_items WHERE id=?",
                            (int(old["id"]),),
                        ).fetchone()
                        conn.execute("COMMIT")
                        return StoreResult("idempotent_noop", _safe_item(old))
                    if sensitivity != "normal" and not sensitive_storage_enabled:
                        raise MemoryStoreError("sensitive_storage_disabled")
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
