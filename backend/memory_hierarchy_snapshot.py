"""Read-only authoritative Atomic Memory snapshot for Phase 4D-B3.

Hierarchy projection needs a complete active snapshot, not the bounded ordinary
retrieval surface.  This reader therefore opens the authoritative relay SQLite
file in ``mode=ro``, validates the frozen Memory schema and fingerprint profile,
re-proves every active row's normalized content/fingerprint, and fails closed if
the active set exceeds the hierarchy planner's hard bound.

No Runtime Authority, write-capable store, provenance text, or sidecar mutation
is owned here.
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from backend import (
    channel_store,
    deployment_config,
    memory_action_ledger,
    memory_candidate_integrity,
    memory_hierarchy_projection as hierarchy,
    memory_policy,
)


_ERROR_CATEGORIES = frozenset({
    "hierarchy_snapshot_configuration_invalid",
    "hierarchy_snapshot_profile_mismatch",
    "hierarchy_snapshot_schema_invalid",
    "hierarchy_snapshot_state_invalid",
    "storage_unavailable",
    "too_many_active_memories",
})


class MemoryHierarchySnapshotError(RuntimeError):
    """Stable, data-free active-snapshot failure."""

    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "hierarchy_snapshot_state_invalid"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except Exception:
            return "hierarchy_snapshot_state_invalid"

    def __repr__(self) -> str:
        return f"MemoryHierarchySnapshotError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHierarchySnapshotError(category)


@dataclass(frozen=True, slots=True, repr=False)
class HierarchyAtomicSnapshotV1:
    """Complete active atomic set; plaintext is intentionally hidden from repr."""

    atomics: tuple[hierarchy.AtomicMemoryProjectionInputV1, ...] = field(
        repr=False
    )

    def __repr__(self) -> str:
        return f"<HierarchyAtomicSnapshotV1 atomics={len(self.atomics)}>"

    @property
    def count(self) -> int:
        return len(self.atomics)


def _timestamp(value: object) -> str:
    if type(value) is not str or not value or len(value) > 128:
        _raise("hierarchy_snapshot_state_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        _raise("hierarchy_snapshot_state_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _raise("hierarchy_snapshot_state_invalid")
    return value


class MemoryHierarchySnapshotReader:
    """Profile-bound mode=ro reader for the complete active Memory snapshot."""

    __slots__ = (
        "_database_path",
        "_fingerprint_hmac_secret",
        "_policy",
        "_profile_verifier",
    )

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        fingerprint_key_id: str,
        fingerprint_hmac_secret: str,
        max_item_chars: int,
        sensitive_storage_enabled: bool,
    ):
        try:
            database = Path(database_path)
            if not database.is_absolute():
                raise ValueError
            if (
                type(fingerprint_key_id) is not str
                or type(fingerprint_hmac_secret) is not str
                or not deployment_config.memory_fingerprint_secret_is_strong(
                    fingerprint_hmac_secret
                )
                or type(max_item_chars) is not int
                or not 64 <= max_item_chars <= 4096
                or type(sensitive_storage_enabled) is not bool
            ):
                raise ValueError
            policy = memory_policy.MemoryPolicy(
                max_item_chars=max_item_chars,
                sensitive_storage_enabled=sensitive_storage_enabled,
            )
            verifier = memory_candidate_integrity.AutomaticCandidateIntegrityVerifier(
                fingerprint_key_id=fingerprint_key_id,
                fingerprint_hmac_secret=fingerprint_hmac_secret,
                max_item_chars=max_item_chars,
            )
        except Exception:
            _raise("hierarchy_snapshot_configuration_invalid")
        self._database_path = str(database)
        self._fingerprint_hmac_secret = fingerprint_hmac_secret
        self._policy = policy
        self._profile_verifier = verifier

    def __repr__(self) -> str:
        return "<MemoryHierarchySnapshotReader>"

    def _connect_read_only(self) -> sqlite3.Connection:
        try:
            return channel_store.connect_read_only(
                self._database_path,
                timeout_seconds=30.0,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            _raise("storage_unavailable")

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        try:
            channel_store.validate_memory_schema(conn)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            _raise("hierarchy_snapshot_schema_invalid")

    def _validate_profile(self, conn: sqlite3.Connection) -> None:
        try:
            self._profile_verifier.verify_profile(conn)
        except memory_candidate_integrity.AutomaticCandidateIntegrityError:
            _raise("hierarchy_snapshot_profile_mismatch")

    def _map_active_row(
        self,
        row: sqlite3.Row,
    ) -> hierarchy.AtomicMemoryProjectionInputV1:
        try:
            memory_key = row["memory_key"]
            kind = row["kind"]
            scope_type = row["scope_type"]
            scope_ref = row["scope_ref"]
            content = row["normalized_content"]
            fingerprint = row["normalized_fingerprint"]
            fingerprint_version = row["fingerprint_version"]
            status = row["status"]
            explicitness = row["explicitness"]
            confidence = row["confidence"]
            sensitivity = row["sensitivity"]
            superseded_by_id = row["superseded_by_id"]
            if (
                type(memory_key) is not str
                or memory_action_ledger.MEMORY_KEY_PATTERN.fullmatch(memory_key)
                is None
                or status != "active"
                or superseded_by_id is not None
                or explicitness not in {"explicit", "inferred"}
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or not 0.0 <= float(confidence) <= 1.0
                or type(fingerprint_version) is not int
                or fingerprint_version != memory_policy.FINGERPRINT_VERSION
                or type(fingerprint) is not bytes
                or len(fingerprint) != memory_policy.HMAC_DIGEST_BYTES
            ):
                _raise("hierarchy_snapshot_state_invalid")
            self._policy.validate_kind(kind)
            self._policy.validate_scope(scope_type, scope_ref)
            normalized = self._policy.validate_content(content, sensitivity)
            if normalized != content:
                _raise("hierarchy_snapshot_state_invalid")
            expected = memory_policy.fingerprint_content(
                self._fingerprint_hmac_secret,
                scope_type=scope_type,
                scope_ref=scope_ref,
                kind=kind,
                normalized_content=content,
            )
            if not memory_policy.secure_digest_equal(fingerprint, expected):
                _raise("hierarchy_snapshot_state_invalid")
            first_observed_at = _timestamp(row["first_observed_at"])
            last_confirmed_at = _timestamp(row["last_confirmed_at"])
            _timestamp(row["created_at"])
            updated_at = _timestamp(row["updated_at"])
            return hierarchy.AtomicMemoryProjectionInputV1(
                memory_key=memory_key,
                kind=kind,
                scope_type=scope_type,
                scope_ref=scope_ref,
                normalized_content=content,
                fingerprint_version=fingerprint_version,
                status="active",
                explicitness=explicitness,
                confidence=float(confidence),
                sensitivity=sensitivity,
                first_observed_at=first_observed_at,
                last_confirmed_at=last_confirmed_at,
                updated_at=updated_at,
            )
        except MemoryHierarchySnapshotError:
            raise
        except memory_policy.MemoryPolicyError:
            _raise("hierarchy_snapshot_state_invalid")
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            _raise("hierarchy_snapshot_state_invalid")

    def load_active_snapshot(self) -> HierarchyAtomicSnapshotV1:
        conn = self._connect_read_only()
        try:
            try:
                conn.execute("BEGIN")
                self._validate_schema(conn)
                self._validate_profile(conn)
                rows = conn.execute(
                    f"""SELECT {memory_candidate_integrity.AUTOMATIC_MEMORY_COLUMNS}
                          FROM memory_items
                         WHERE status='active'
                         ORDER BY id
                         LIMIT ?""",
                    (hierarchy.MAX_ATOMICS + 1,),
                ).fetchall()
                if len(rows) > hierarchy.MAX_ATOMICS:
                    _raise("too_many_active_memories")
                atomics = tuple(self._map_active_row(row) for row in rows)
                # Reuse the hierarchy contract as the final shape validator.
                hierarchy._validate_atomics(atomics)
                conn.execute("COMMIT")
                return HierarchyAtomicSnapshotV1(atomics=atomics)
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
        except MemoryHierarchySnapshotError:
            raise
        except (OSError, sqlite3.Error):
            _raise("storage_unavailable")
        except Exception:
            _raise("hierarchy_snapshot_state_invalid")
        finally:
            conn.close()
