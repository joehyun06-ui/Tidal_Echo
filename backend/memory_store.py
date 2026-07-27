"""Transactional SQLite store for explicit derived memories."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

try:
    from . import (
        channel_store,
        memory_action_ledger,
        memory_policy,
        memory_runtime,
    )
except ImportError:  # support direct module execution in local tooling
    import channel_store
    import memory_action_ledger
    import memory_policy
    import memory_runtime


class MemoryStoreError(RuntimeError):
    """A fixed, data-free storage error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class StoreResult:
    outcome: str
    item: dict | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _ForgetTargetMetadataV1:
    memory_id: int
    memory_key: str = field(repr=False)
    kind: str
    scope_type: str
    scope_ref: str = field(repr=False)
    status: str
    sensitivity: str
    fingerprint_version: int
    normalized_fingerprint: bytes | None = field(repr=False)
    superseded_by_id: int | None = field(repr=False)
    updated_at: str


@dataclass(frozen=True, repr=False)
class _CanonicalAction:
    canonical_message_id: int
    channel: str
    source: str
    role: str


@dataclass(frozen=True, repr=False)
class _PreparedGrant:
    canonical: _CanonicalAction
    evidence_event_id: int
    created_in_transaction: bool


@dataclass(frozen=True, repr=False)
class _ValidatedSource:
    canonical_message_id: int
    evidence_event_id: int
    channel: str
    source: str
    evidence_role: str
    evidence_type: str


class _GrantKind(Enum):
    EXPLICIT_USER_MEMORY = (
        memory_runtime.ACTION_REMEMBER_USER,
        "explicit_user_memory", "user", "real", "user", "memory_admin",
    )
    EXPLICIT_USER_CORRECTION = (
        memory_runtime.ACTION_CORRECT_USER,
        "explicit_user_correction", "user", "real", "user", "memory_admin",
    )
    EXPLICIT_USER_FORGET = (
        memory_runtime.ACTION_FORGET_USER,
        "user_forget", "user", "real", "user", "memory_admin",
    )
    CONFIRMED_PROJECT_DECISION = (
        memory_runtime.ACTION_CONFIRM_DECISION,
        "confirmed_project_decision", "user", "real", "project", "memory_admin",
    )
    ASSISTANT_EXPERIENCE = (
        memory_runtime.ACTION_ASSISTANT_EXPERIENCE,
        "assistant_experience", "assistant", "real", "assistant", "assistant_runtime",
    )

    @property
    def action_type(self) -> str:
        return str(self.value[0])

    @property
    def evidence_type(self) -> str:
        return str(self.value[1])

    @property
    def expected_role(self) -> str:
        return str(self.value[2])

    @property
    def reality_scope(self) -> str:
        return str(self.value[3])

    @property
    def subject_scope(self) -> str:
        return str(self.value[4])

    @property
    def component(self) -> str:
        return str(self.value[5])


def _single_source(
    sources: Sequence[memory_policy.ProvenanceInput],
) -> memory_policy.ProvenanceInput:
    if len(sources) != 1:
        raise MemoryStoreError("invalid_provenance")
    return sources[0]


def _binding_source(
    sources: Iterable[memory_policy.ProvenanceInput],
) -> tuple[memory_policy.ProvenanceInput, tuple[memory_policy.ProvenanceInput, ...]]:
    try:
        materialized = tuple(sources)
    except TypeError:
        raise MemoryStoreError("invalid_provenance") from None
    source = _single_source(materialized)
    if (
        not isinstance(source, memory_policy.ProvenanceInput)
        or not isinstance(source.canonical_message_id, int)
        or isinstance(source.canonical_message_id, bool)
        or source.canonical_message_id <= 0
    ):
        raise MemoryStoreError("invalid_provenance")
    return source, materialized


MAX_CANONICAL_META_BYTES = 16 * 1024
MAX_CANONICAL_META_KEYS = 64
MAX_CANONICAL_META_DEPTH = 8
MAX_CANONICAL_META_STRING_CHARS = 4096
_SAFE_META_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_SENSITIVITY_RANK = {"normal": 0, "sensitive": 1, "restricted": 2}
_FORGET_TARGET_COLUMNS = """
    id,memory_key,kind,scope_type,scope_ref,status,sensitivity,
    fingerprint_version,normalized_fingerprint,superseded_by_id,updated_at
"""
_FORGET_RESULT_COLUMNS = """
    id,memory_key,kind,scope_type,scope_ref,status,sensitivity,
    fingerprint_version,normalized_fingerprint,superseded_by_id,updated_at,
    explicitness,confidence,first_observed_at,last_confirmed_at,created_at
"""
_PROFILE_STATE_TABLES = (
    "memory_items",
    "memory_sources",
    "memory_suppressions",
    "memory_evidence_events",
)


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


def _normalize_source(raw: object) -> str:
    if raw is None or raw == "":
        return ""
    if (
        not isinstance(raw, str)
        or raw != raw.strip()
        or _SAFE_META_VALUE.fullmatch(raw) is None
    ):
        raise MemoryStoreError("invalid_provenance")
    return raw


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


def _forget_target_metadata(
    row: sqlite3.Row,
    *,
    expected_memory_key: str,
) -> _ForgetTargetMetadataV1:
    try:
        fingerprint = row["normalized_fingerprint"]
        status = row["status"]
        superseded_by_id = row["superseded_by_id"]
        if (
            type(row["id"]) is not int
            or row["id"] <= 0
            or not isinstance(row["memory_key"], str)
            or memory_action_ledger.MEMORY_KEY_PATTERN.fullmatch(
                row["memory_key"]
            )
            is None
            or row["memory_key"] != expected_memory_key
            or row["kind"] not in memory_policy.KINDS
            or row["scope_type"] not in memory_policy.SCOPE_TYPES
            or not isinstance(row["scope_ref"], str)
            or status not in {"active", "forgotten"}
            or row["sensitivity"] not in memory_policy.SENSITIVITIES
            or type(row["fingerprint_version"]) is not int
            or row["fingerprint_version"] != memory_policy.FINGERPRINT_VERSION
            or superseded_by_id is not None
            or not isinstance(row["updated_at"], str)
            or not row["updated_at"]
            or (
                status == "active"
                and (
                    type(fingerprint) is not bytes
                    or len(fingerprint) != memory_policy.HMAC_DIGEST_BYTES
                )
            )
            or (status == "forgotten" and fingerprint is not None)
        ):
            raise MemoryStoreError("invalid_state")
        if row["scope_type"] == "global_user":
            if row["scope_ref"] != "":
                raise MemoryStoreError("invalid_state")
        elif (
            memory_action_ledger.SCOPE_REF_PATTERN.fullmatch(
                row["scope_ref"]
            )
            is None
            or (
                row["scope_type"] == "channel"
                and row["scope_ref"] not in memory_policy.KNOWN_CHANNELS
            )
        ):
            raise MemoryStoreError("invalid_state")
    except MemoryStoreError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        raise MemoryStoreError("invalid_state") from None
    return _ForgetTargetMetadataV1(
        memory_id=int(row["id"]),
        memory_key=row["memory_key"],
        kind=row["kind"],
        scope_type=row["scope_type"],
        scope_ref=row["scope_ref"],
        status=status,
        sensitivity=row["sensitivity"],
        fingerprint_version=int(row["fingerprint_version"]),
        normalized_fingerprint=fingerprint,
        superseded_by_id=superseded_by_id,
        updated_at=row["updated_at"],
    )


def _safe_forgotten_item(
    row: sqlite3.Row,
    metadata: _ForgetTargetMetadataV1,
) -> dict:
    if type(metadata) is not _ForgetTargetMetadataV1:
        raise MemoryStoreError("invalid_state")
    try:
        confidence = row["confidence"]
        if (
            row["id"] != metadata.memory_id
            or row["memory_key"] != metadata.memory_key
            or row["kind"] != metadata.kind
            or row["scope_type"] != metadata.scope_type
            or row["scope_ref"] != metadata.scope_ref
            or row["status"] != "forgotten"
            or row["sensitivity"] != metadata.sensitivity
            or row["fingerprint_version"] != metadata.fingerprint_version
            or row["normalized_fingerprint"] is not None
            or row["superseded_by_id"] is not None
            or row["updated_at"] != metadata.updated_at
            or row["explicitness"] not in {"explicit", "inferred"}
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
            or not all(
                isinstance(row[name], str) and bool(row[name])
                for name in (
                    "first_observed_at",
                    "last_confirmed_at",
                    "created_at",
                )
            )
        ):
            raise MemoryStoreError("invalid_state")
    except MemoryStoreError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        raise MemoryStoreError("invalid_state") from None
    return {
        "memory_key": metadata.memory_key,
        "kind": metadata.kind,
        "scope_type": metadata.scope_type,
        "scope_ref": metadata.scope_ref,
        "normalized_content": None,
        "fingerprint_version": metadata.fingerprint_version,
        "status": "forgotten",
        "explicitness": row["explicitness"],
        "confidence": float(confidence),
        "sensitivity": metadata.sensitivity,
        "first_observed_at": row["first_observed_at"],
        "last_confirmed_at": row["last_confirmed_at"],
        "created_at": row["created_at"],
        "updated_at": metadata.updated_at,
    }


class MemoryStore:
    """Final enforcement point for all Memory Core production writes."""

    HARD_MAX_ITEMS = 100

    def __init__(self, path: str, authority: object):
        try:
            runtime_policy = memory_runtime.require_runtime_authority(authority)
        except memory_runtime.MemoryRuntimeError as error:
            raise MemoryStoreError(error.category) from None
        self.path = path
        self._authority = authority
        self._runtime_policy = runtime_policy

    def _trusted_policy(self) -> memory_policy.MemoryPolicy:
        return memory_policy.MemoryPolicy(
            max_item_chars=self._runtime_policy.max_item_chars,
            sensitive_storage_enabled=self._runtime_policy.sensitive_storage_enabled,
        )

    @property
    def policy(self) -> memory_policy.MemoryPolicy:
        """Return a preflight policy copy; writes reconstruct their trusted copy."""
        return self._trusted_policy()

    def validate_schema(self) -> bool:
        try:
            with channel_store.connect(self.path) as conn:
                channel_store.validate_memory_action_schema(conn)
            return True
        except (OSError, sqlite3.Error, ValueError):
            return False

    def _action_unit_of_work(self):
        """Create the internal root transaction for a reviewed composition path."""
        self._require_write_runtime()
        try:
            runtime_policy = memory_runtime.require_runtime_authority(
                self._authority
            )
        except memory_runtime.MemoryRuntimeError as error:
            raise MemoryStoreError(error.category) from None
        if runtime_policy is not self._runtime_policy:
            raise MemoryStoreError("runtime_authority_invalid")
        return memory_action_ledger._new_unit_of_work(
            store=self,
            secret=self._runtime_policy.fingerprint_hmac_secret,
        )

    def _write_connection(self, transaction: object | None):
        if transaction is None:
            return channel_store.connect(self.path)
        if not isinstance(
            transaction,
            memory_action_ledger._MemoryActionUnitOfWork,
        ):
            raise MemoryStoreError("transaction_context_invalid")
        try:
            return transaction._store_connection(self)
        except memory_action_ledger.MemoryActionLedgerError as error:
            raise MemoryStoreError(error.category) from None

    def _get_forget_target_metadata(
        self,
        memory_key: str,
        *,
        _transaction: object | None = None,
    ) -> _ForgetTargetMetadataV1 | None:
        self._require_write_runtime()
        if (
            not isinstance(memory_key, str)
            or memory_action_ledger.MEMORY_KEY_PATTERN.fullmatch(memory_key)
            is None
        ):
            raise MemoryStoreError("invalid_memory_key")
        try:
            if _transaction is None:
                with channel_store.connect(self.path) as conn:
                    row = conn.execute(
                        f"""SELECT {_FORGET_TARGET_COLUMNS}
                            FROM memory_items WHERE memory_key=?""",
                        (memory_key,),
                    ).fetchone()
            else:
                if (
                    type(_transaction)
                    is not memory_action_ledger._MemoryActionUnitOfWork
                    or _transaction._store is not self
                ):
                    raise MemoryStoreError("transaction_context_invalid")
                row = _transaction._execute(
                    f"""SELECT {_FORGET_TARGET_COLUMNS}
                        FROM memory_items WHERE memory_key=?""",
                    (memory_key,),
                ).fetchone()
            if row is None:
                return None
            return _forget_target_metadata(
                row,
                expected_memory_key=memory_key,
            )
        except MemoryStoreError:
            raise
        except memory_action_ledger.MemoryActionLedgerError as error:
            raise MemoryStoreError(error.category) from None
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def _validate_forget_target_binding(
        self,
        metadata: _ForgetTargetMetadataV1,
        *,
        _transaction: object,
    ) -> None:
        if (
            type(metadata) is not _ForgetTargetMetadataV1
            or type(_transaction)
            is not memory_action_ledger._MemoryActionUnitOfWork
            or _transaction._store is not self
        ):
            raise MemoryStoreError("transaction_context_invalid")
        binding = _transaction._binding
        if (
            type(binding)
            is not memory_action_ledger.MemoryActionRequestBinding
            or binding.action_kind != "forget"
            or binding.target_memory_key != metadata.memory_key
            or binding.kind != metadata.kind
            or binding.scope_type != metadata.scope_type
            or binding.scope_ref != metadata.scope_ref
            or binding.sensitivity != metadata.sensitivity
            or binding.normalized_content is not None
        ):
            raise MemoryStoreError("request_binding_conflict")

    def _defer_action_to_transaction(
        self,
        transaction: object | None,
        action_id: str | None,
        *,
        consumed: bool,
        result: StoreResult | None,
        suppression_ids: tuple[int, ...],
    ) -> str | None:
        if transaction is None or action_id is None or not consumed:
            return action_id
        if result is None:
            raise MemoryStoreError("invalid_state")
        try:
            transaction._record_store_outcome(
                store=self,
                action_id=action_id,
                store_result=result,
                suppression_ids=suppression_ids,
            )
            transaction._defer_action(action_id)
        except memory_action_ledger.MemoryActionLedgerError as error:
            if action_id not in transaction._deferred_actions:
                self._finish_action(action_id, consumed=False)
            raise MemoryStoreError(error.category) from None
        return None

    def _require_write_runtime(self) -> None:
        try:
            policy = memory_runtime.require_runtime_authority(self._authority)
        except memory_runtime.MemoryRuntimeError as error:
            raise MemoryStoreError(error.category) from None
        if policy is not self._runtime_policy:
            raise MemoryStoreError("runtime_authority_invalid")
        if not policy.enabled:
            raise MemoryStoreError("feature_disabled")
        if not policy.configuration_valid:
            raise MemoryStoreError("memory_configuration_invalid")
        if not policy.explicit_writes_enabled:
            raise MemoryStoreError("explicit_writes_disabled")
        if (
            policy.normalization_version != memory_policy.NORMALIZATION_VERSION
            or policy.fingerprint_version != memory_policy.FINGERPRINT_VERSION
            or policy.fingerprint_domain != memory_policy.FINGERPRINT_DOMAIN
        ):
            raise MemoryStoreError("memory_configuration_invalid")
        try:
            memory_policy.fingerprint_profile_check(
                policy.fingerprint_hmac_secret
            )
        except memory_policy.MemoryPolicyError:
            raise MemoryStoreError("memory_configuration_invalid") from None

    def _profile_parameters(self) -> tuple[str, bytes, int, int]:
        try:
            return (
                self._runtime_policy.fingerprint_key_id,
                memory_policy.fingerprint_profile_check(
                    self._runtime_policy.fingerprint_hmac_secret
                ),
                self._runtime_policy.normalization_version,
                self._runtime_policy.fingerprint_version,
            )
        except memory_policy.MemoryPolicyError:
            raise MemoryStoreError("memory_configuration_invalid") from None

    @staticmethod
    def _profile_matches(
        row: sqlite3.Row,
        *,
        key_id: str,
        key_check: bytes,
        normalization_version: int,
        fingerprint_version: int,
    ) -> bool:
        try:
            singleton = row["singleton"]
            stored_key_id = row["key_id"]
            stored_check = row["key_check"]
            stored_normalization = row["normalization_version"]
            stored_fingerprint = row["fingerprint_version"]
            created_at = row["created_at"]
            updated_at = row["updated_at"]
        except (IndexError, KeyError):
            return False
        return (
            type(singleton) is int
            and singleton == 1
            and isinstance(stored_key_id, str)
            and bool(stored_key_id)
            and hmac.compare_digest(stored_key_id, key_id)
            and memory_policy.secure_digest_equal(stored_check, key_check)
            and type(stored_normalization) is int
            and stored_normalization == normalization_version
            and type(stored_fingerprint) is int
            and stored_fingerprint == fingerprint_version
            and isinstance(created_at, str)
            and bool(created_at)
            and isinstance(updated_at, str)
            and bool(updated_at)
        )

    @staticmethod
    def _memory_state_count(conn: sqlite3.Connection) -> int:
        return sum(
            int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in _PROFILE_STATE_TABLES
        )

    def _validate_or_initialize_profile(
        self,
        conn: sqlite3.Connection,
        *,
        initialize: bool,
    ) -> None:
        key_id, key_check, normalization_version, fingerprint_version = (
            self._profile_parameters()
        )
        rows = conn.execute(
            "SELECT * FROM memory_fingerprint_profile ORDER BY singleton"
        ).fetchall()
        if len(rows) > 1:
            raise MemoryStoreError("memory_fingerprint_profile_mismatch")
        if not rows:
            if self._memory_state_count(conn):
                raise MemoryStoreError("memory_fingerprint_profile_mismatch")
            if initialize:
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
            return
        if not self._profile_matches(
            rows[0],
            key_id=key_id,
            key_check=key_check,
            normalization_version=normalization_version,
            fingerprint_version=fingerprint_version,
        ):
            raise MemoryStoreError("memory_fingerprint_profile_mismatch")

    def validate_runtime_profile_state(self) -> bool:
        try:
            with channel_store.connect(self.path) as conn:
                self._validate_or_initialize_profile(conn, initialize=False)
            return True
        except MemoryStoreError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    @staticmethod
    def _derive_evidence_role(direction: object, kind: object) -> str:
        if direction == "in" and kind in {"user", "voice"}:
            return "user"
        if direction == "out" and kind in {"reply", "voice"}:
            return "assistant"
        raise MemoryStoreError("invalid_provenance")

    @classmethod
    def _read_canonical_action(
        cls,
        conn: sqlite3.Connection,
        *,
        canonical_message_id: int,
        expected_role: str,
    ) -> _CanonicalAction:
        row = conn.execute(
            "SELECT direction,kind,meta FROM messages WHERE id=?",
            (canonical_message_id,),
        ).fetchone()
        if row is None:
            raise MemoryStoreError("invalid_provenance")
        meta = _load_bounded_meta(row["meta"])
        channel = meta.get("channel")
        if (
            not isinstance(channel, str)
            or not channel
            or _SAFE_META_VALUE.fullmatch(channel) is None
            or channel not in memory_policy.KNOWN_CHANNELS
        ):
            raise MemoryStoreError("invalid_provenance")
        source = _normalize_source(meta.get("source"))
        role = cls._derive_evidence_role(row["direction"], row["kind"])
        if role != expected_role:
            raise MemoryStoreError("unsupported_evidence")
        return _CanonicalAction(
            canonical_message_id=canonical_message_id,
            channel=channel,
            source=source,
            role=role,
        )

    @classmethod
    def _prepare_grant(
        cls,
        conn: sqlite3.Connection,
        *,
        canonical_message_id: int,
        grant_kind: _GrantKind,
        action_id: str,
        stamp: str,
    ) -> _PreparedGrant:
        canonical = cls._read_canonical_action(
            conn,
            canonical_message_id=canonical_message_id,
            expected_role=grant_kind.expected_role,
        )
        if conn.execute(
            """SELECT 1 FROM memory_evidence_events
               WHERE canonical_message_id=? OR action_id=?""",
            (canonical_message_id, action_id),
        ).fetchone() is not None:
            raise MemoryStoreError("authorization_replayed")
        cursor = conn.execute(
            """INSERT INTO memory_evidence_events
               (canonical_message_id,action_id,action_type,action_binding_version,
                evidence_type,reality_scope,subject_scope,created_by_component,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                canonical_message_id,
                action_id,
                grant_kind.action_type,
                memory_runtime.ACTION_BINDING_VERSION,
                grant_kind.evidence_type,
                grant_kind.reality_scope,
                grant_kind.subject_scope,
                grant_kind.component,
                stamp,
            ),
        )
        return _PreparedGrant(
            canonical=canonical,
            evidence_event_id=int(cursor.lastrowid),
            created_in_transaction=True,
        )

    @classmethod
    def _grant_action_sources(
        cls,
        conn: sqlite3.Connection,
        *,
        sources: Sequence[memory_policy.ProvenanceInput],
        grant_kind: _GrantKind,
        action_id: str,
        stamp: str,
    ) -> tuple[_PreparedGrant, ...]:
        source = _single_source(sources)
        return (cls._prepare_grant(
            conn,
            canonical_message_id=source.canonical_message_id,
            grant_kind=grant_kind,
            action_id=action_id,
            stamp=stamp,
        ),)

    @classmethod
    def _bind_prepared_grants(
        cls,
        conn: sqlite3.Connection,
        *,
        grants: Sequence[_PreparedGrant],
        memory_id: int,
        grant_kind: _GrantKind,
    ) -> tuple[_ValidatedSource, ...]:
        validated: list[_ValidatedSource] = []
        for grant in grants:
            canonical = cls._read_canonical_action(
                conn,
                canonical_message_id=grant.canonical.canonical_message_id,
                expected_role=grant_kind.expected_role,
            )
            if canonical != grant.canonical:
                raise MemoryStoreError("invalid_provenance")
            links = conn.execute(
                """SELECT memory_id,channel,source,evidence_role,evidence_type
                   FROM memory_sources WHERE evidence_event_id=? ORDER BY id""",
                (grant.evidence_event_id,),
            ).fetchall()
            if grant.created_in_transaction:
                if links:
                    raise MemoryStoreError("unsupported_evidence")
            elif (
                len(links) != 1
                or int(links[0]["memory_id"]) != memory_id
                or links[0]["channel"] != canonical.channel
                or links[0]["source"] != canonical.source
                or links[0]["evidence_role"] != canonical.role
                or links[0]["evidence_type"] != grant_kind.evidence_type
            ):
                raise MemoryStoreError("unsupported_evidence")
            validated.append(_ValidatedSource(
                canonical_message_id=canonical.canonical_message_id,
                evidence_event_id=grant.evidence_event_id,
                channel=canonical.channel,
                source=canonical.source,
                evidence_role=canonical.role,
                evidence_type=grant_kind.evidence_type,
            ))
        return tuple(validated)

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
            if (
                isinstance(stored, bytes)
                and memory_policy.secure_digest_equal(stored, fingerprint)
            ):
                matched = row
        return matched

    @staticmethod
    def _matching_suppression_ids(
        conn: sqlite3.Connection,
        *,
        scope_type: str,
        scope_ref: str,
        kind: str,
        fingerprint_version: int,
        fingerprint: bytes,
    ) -> tuple[int, ...]:
        rows = conn.execute(
            """SELECT id,normalized_fingerprint FROM memory_suppressions
               WHERE scope_type=? AND scope_ref=? AND kind=? AND fingerprint_version=?
               ORDER BY id""",
            (scope_type, scope_ref, kind, fingerprint_version),
        ).fetchall()
        matched: list[int] = []
        for row in rows:
            stored = row["normalized_fingerprint"]
            current = (
                isinstance(stored, bytes)
                and memory_policy.secure_digest_equal(stored, fingerprint)
            )
            if current:
                matched.append(int(row["id"]))
        return tuple(matched)

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
    ) -> int:
        conn.execute(
            """INSERT INTO memory_suppressions
               (scope_type,scope_ref,kind,normalized_fingerprint,fingerprint_version,
                reason_category,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(scope_type,scope_ref,kind,fingerprint_version,normalized_fingerprint)
               DO NOTHING""",
            (
                scope_type,
                scope_ref,
                kind,
                fingerprint,
                fingerprint_version,
                reason_category,
                stamp,
            ),
        )
        rows = conn.execute(
            """SELECT id,normalized_fingerprint FROM memory_suppressions
               WHERE scope_type=? AND scope_ref=? AND kind=?
                 AND fingerprint_version=? ORDER BY id""",
            (scope_type, scope_ref, kind, fingerprint_version),
        ).fetchall()
        matched_ids = tuple(
            int(row["id"])
            for row in rows
            if (
                isinstance(row["normalized_fingerprint"], bytes)
                and memory_policy.secure_digest_equal(
                    row["normalized_fingerprint"],
                    fingerprint,
                )
            )
        )
        if len(matched_ids) != 1:
            raise MemoryStoreError("conflict")
        return matched_ids[0]

    @staticmethod
    def _forgotten_target_suppression_ids(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> tuple[int, ...]:
        rows = conn.execute(
            """SELECT id FROM memory_suppressions
               WHERE scope_type=? AND scope_ref=? AND kind=?
                 AND fingerprint_version=?
                 AND reason_category='user_forget'
                 AND created_at=? ORDER BY id""",
            (
                row["scope_type"],
                row["scope_ref"],
                row["kind"],
                int(row["fingerprint_version"]),
                row["updated_at"],
            ),
        ).fetchall()
        return tuple(int(value["id"]) for value in rows)

    @staticmethod
    def _translate_sqlite_error(error: Exception) -> MemoryStoreError:
        if isinstance(error, sqlite3.IntegrityError):
            return MemoryStoreError("conflict")
        return MemoryStoreError("storage_unavailable")

    def _begin_action(
        self,
        authorization: object | None,
        binding: memory_runtime.MemoryActionBinding,
        transaction: object | None = None,
    ) -> str:
        if transaction is not None:
            try:
                transaction._validate_store_action(self, binding)
            except memory_action_ledger.MemoryActionLedgerError as error:
                raise MemoryStoreError(error.category) from None
        try:
            return memory_runtime.begin_action_consumption(
                self._authority,
                authorization,
                expected_binding=binding,
            )
        except memory_runtime.MemoryRuntimeError as error:
            raise MemoryStoreError(error.category) from None

    def _finish_action(self, action_id: str | None, *, consumed: bool) -> None:
        if action_id is None:
            return
        try:
            memory_runtime.finish_action_consumption(
                self._authority,
                action_id,
                consumed=consumed,
            )
        except memory_runtime.MemoryRuntimeError as error:
            raise MemoryStoreError(error.category) from None

    def _create_from_action(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        sources: Iterable[memory_policy.ProvenanceInput],
        grant_kind: _GrantKind,
        authorization: object | None,
        _transaction: object | None = None,
    ) -> StoreResult:
        self._require_write_runtime()
        policy = self._trusted_policy()
        action_id: str | None = None
        consumed = False
        result: StoreResult | None = None
        suppression_ids: tuple[int, ...] = ()
        try:
            with self._write_connection(_transaction) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._require_write_runtime()
                    if authorization is None:
                        raise MemoryStoreError("authorization_required")
                    source, source_inputs = _binding_source(sources)
                    normalized_binding_content = memory_policy.normalize_content(
                        content,
                        max_chars=self._runtime_policy.max_item_chars,
                    )
                    binding = memory_runtime.MemoryActionBinding(
                        action_type=grant_kind.action_type,
                        canonical_message_id=source.canonical_message_id,
                        kind=kind,
                        scope_type=scope_type,
                        scope_ref=scope_ref,
                        normalized_content=normalized_binding_content,
                        sensitivity=sensitivity,
                    )
                    action_id = self._begin_action(
                        authorization,
                        binding,
                        transaction=_transaction,
                    )
                    normalized_content, validated_inputs = (
                        policy.validate_explicit_create(
                            kind=kind,
                            scope_type=scope_type,
                            scope_ref=scope_ref,
                            content=content,
                            sensitivity=sensitivity,
                            sources=source_inputs,
                            allow_existing_reclassification=True,
                        )
                    )
                    if normalized_content != normalized_binding_content:
                        raise MemoryStoreError("authorization_invalid")
                    self._validate_or_initialize_profile(conn, initialize=True)
                    stamp = channel_store.now_iso()
                    prepared_grants = self._grant_action_sources(
                        conn,
                        sources=validated_inputs,
                        grant_kind=grant_kind,
                        action_id=action_id,
                        stamp=stamp,
                    )
                    fingerprint = memory_policy.fingerprint_content(
                        self._runtime_policy.fingerprint_hmac_secret,
                        scope_type=scope_type,
                        scope_ref=scope_ref,
                        kind=kind,
                        normalized_content=normalized_content,
                    )
                    suppression_ids = self._matching_suppression_ids(
                        conn,
                        scope_type=scope_type,
                        scope_ref=scope_ref,
                        kind=kind,
                        fingerprint=fingerprint,
                        fingerprint_version=memory_policy.FINGERPRINT_VERSION,
                    )
                    if suppression_ids:
                        conn.execute("ROLLBACK")
                        consumed = True
                        result = StoreResult("suppressed")
                    else:
                        existing = self._find_live_by_fingerprint(
                            conn,
                            scope_type=scope_type,
                            scope_ref=scope_ref,
                            kind=kind,
                            fingerprint=fingerprint,
                            fingerprint_version=self._runtime_policy.fingerprint_version,
                        )
                        if existing is not None:
                            if existing["normalized_content"] != normalized_content:
                                raise MemoryStoreError("conflict")
                            existing_rank = _SENSITIVITY_RANK.get(existing["sensitivity"])
                            requested_rank = _SENSITIVITY_RANK.get(sensitivity)
                            if existing_rank is None or requested_rank is None:
                                raise MemoryStoreError("invalid_state")
                            sources_to_insert = self._bind_prepared_grants(
                                conn,
                                grants=prepared_grants,
                                memory_id=int(existing["id"]),
                                grant_kind=grant_kind,
                            )
                            if requested_rank > existing_rank:
                                conn.execute(
                                    """UPDATE memory_items
                                       SET sensitivity=?,last_confirmed_at=?,updated_at=?
                                       WHERE id=?""",
                                    (sensitivity, stamp, stamp, int(existing["id"])),
                                )
                            self._insert_sources(
                                conn, int(existing["id"]), sources_to_insert, stamp,
                            )
                            existing = conn.execute(
                                "SELECT * FROM memory_items WHERE id=?",
                                (int(existing["id"]),),
                            ).fetchone()
                            conn.execute("COMMIT")
                            consumed = True
                            result = StoreResult(
                                "idempotent_existing", _safe_item(existing),
                            )
                        else:
                            if (
                                sensitivity != "normal"
                                and not self._runtime_policy.sensitive_storage_enabled
                            ):
                                raise MemoryStoreError("sensitive_storage_disabled")
                            memory_key = secrets.token_urlsafe(24)
                            cursor = conn.execute(
                                """INSERT INTO memory_items
                                   (memory_key,kind,scope_type,scope_ref,normalized_content,
                                    normalized_fingerprint,fingerprint_version,status,explicitness,
                                    confidence,sensitivity,first_observed_at,last_confirmed_at,
                                    superseded_by_id,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,?,'active','explicit',1.0,?,?,?,NULL,?,?)""",
                                (
                                    memory_key,
                                    kind,
                                    scope_type,
                                    scope_ref,
                                    normalized_content,
                                    fingerprint,
                                    self._runtime_policy.fingerprint_version,
                                    sensitivity,
                                    stamp,
                                    stamp,
                                    stamp,
                                    stamp,
                                ),
                            )
                            memory_id = int(cursor.lastrowid)
                            sources_to_insert = self._bind_prepared_grants(
                                conn,
                                grants=prepared_grants,
                                memory_id=memory_id,
                                grant_kind=grant_kind,
                            )
                            self._insert_sources(
                                conn, memory_id, sources_to_insert, stamp,
                            )
                            row = conn.execute(
                                "SELECT * FROM memory_items WHERE id=?", (memory_id,),
                            ).fetchone()
                            conn.execute("COMMIT")
                            consumed = True
                            result = StoreResult("created", _safe_item(row))
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            return result
        except MemoryStoreError:
            raise
        except memory_action_ledger.MemoryActionLedgerError as error:
            raise MemoryStoreError(error.category) from None
        except memory_policy.MemoryPolicyError as error:
            raise MemoryStoreError(error.category) from None
        except (OSError, sqlite3.Error, ValueError) as error:
            raise self._translate_sqlite_error(error) from None
        finally:
            action_id = self._defer_action_to_transaction(
                _transaction,
                action_id,
                consumed=consumed,
                result=result,
                suppression_ids=suppression_ids,
            )
            self._finish_action(action_id, consumed=consumed)

    def create_explicit_memory_from_user_action(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        sources: Iterable[memory_policy.ProvenanceInput],
        authorization: object | None = None,
        _transaction: object | None = None,
    ) -> StoreResult:
        if kind in {"assistant_experience", "decision"}:
            raise MemoryStoreError("unsupported_evidence")
        return self._create_from_action(
            kind=kind,
            scope_type=scope_type,
            scope_ref=scope_ref,
            content=content,
            sensitivity=sensitivity,
            sources=sources,
            grant_kind=_GrantKind.EXPLICIT_USER_MEMORY,
            authorization=authorization,
            _transaction=_transaction,
        )

    def create_confirmed_project_decision_from_action(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        sources: Iterable[memory_policy.ProvenanceInput],
        authorization: object | None = None,
        _transaction: object | None = None,
    ) -> StoreResult:
        if kind != "decision":
            raise MemoryStoreError("unsupported_evidence")
        return self._create_from_action(
            kind=kind,
            scope_type=scope_type,
            scope_ref=scope_ref,
            content=content,
            sensitivity=sensitivity,
            sources=sources,
            grant_kind=_GrantKind.CONFIRMED_PROJECT_DECISION,
            authorization=authorization,
            _transaction=_transaction,
        )

    def create_assistant_experience_from_action(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        sources: Iterable[memory_policy.ProvenanceInput],
        authorization: object | None = None,
        _transaction: object | None = None,
    ) -> StoreResult:
        if kind != "assistant_experience":
            raise MemoryStoreError("unsupported_evidence")
        return self._create_from_action(
            kind=kind,
            scope_type=scope_type,
            scope_ref=scope_ref,
            content=content,
            sensitivity=sensitivity,
            sources=sources,
            grant_kind=_GrantKind.ASSISTANT_EXPERIENCE,
            authorization=authorization,
            _transaction=_transaction,
        )

    def correct_memory_from_user_action(
        self,
        *,
        memory_key: str,
        content: str,
        sensitivity: str,
        sources: Iterable[memory_policy.ProvenanceInput],
        authorization: object | None = None,
        _transaction: object | None = None,
    ) -> StoreResult:
        self._require_write_runtime()
        policy = self._trusted_policy()
        action_id: str | None = None
        consumed = False
        result: StoreResult | None = None
        suppression_ids: tuple[int, ...] = ()
        try:
            with self._write_connection(_transaction) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._require_write_runtime()
                    if authorization is None:
                        raise MemoryStoreError("authorization_required")
                    old = conn.execute(
                        "SELECT * FROM memory_items WHERE memory_key=?",
                        (memory_key,),
                    ).fetchone()
                    if old is None:
                        raise MemoryStoreError("not_found")
                    if old["status"] != "active":
                        raise MemoryStoreError("invalid_state")
                    if old["kind"] == "assistant_experience":
                        raise MemoryStoreError("unsupported_evidence")
                    source, source_inputs = _binding_source(sources)
                    normalized_binding_content = memory_policy.normalize_content(
                        content,
                        max_chars=self._runtime_policy.max_item_chars,
                    )
                    binding = memory_runtime.MemoryActionBinding(
                        action_type=memory_runtime.ACTION_CORRECT_USER,
                        canonical_message_id=source.canonical_message_id,
                        kind=old["kind"],
                        scope_type=old["scope_type"],
                        scope_ref=old["scope_ref"],
                        normalized_content=normalized_binding_content,
                        sensitivity=sensitivity,
                        memory_key=memory_key,
                    )
                    action_id = self._begin_action(
                        authorization,
                        binding,
                        transaction=_transaction,
                    )
                    policy.validate_kind(old["kind"])
                    policy.validate_scope(old["scope_type"], old["scope_ref"])
                    normalized_content = policy.validate_content(
                        content,
                        sensitivity,
                        allow_existing_reclassification=True,
                    )
                    if normalized_content != normalized_binding_content:
                        raise MemoryStoreError("authorization_invalid")
                    validated_inputs = policy.validate_provenance_inputs(
                        old["kind"], source_inputs,
                    )
                    existing_rank = _SENSITIVITY_RANK.get(old["sensitivity"])
                    requested_rank = _SENSITIVITY_RANK.get(sensitivity)
                    if existing_rank is None or requested_rank is None:
                        raise MemoryStoreError("invalid_state")
                    if requested_rank < existing_rank:
                        raise MemoryStoreError("sensitivity_downgrade")
                    self._validate_or_initialize_profile(conn, initialize=True)
                    stamp = channel_store.now_iso()
                    prepared_grants = self._grant_action_sources(
                        conn,
                        sources=validated_inputs,
                        grant_kind=_GrantKind.EXPLICIT_USER_CORRECTION,
                        action_id=action_id,
                        stamp=stamp,
                    )
                    fingerprint = memory_policy.fingerprint_content(
                        self._runtime_policy.fingerprint_hmac_secret,
                        scope_type=old["scope_type"],
                        scope_ref=old["scope_ref"],
                        kind=old["kind"],
                        normalized_content=normalized_content,
                    )
                    old_fingerprint = old["normalized_fingerprint"]
                    if not isinstance(old_fingerprint, bytes):
                        raise MemoryStoreError("invalid_state")
                    if memory_policy.secure_digest_equal(
                        old_fingerprint, fingerprint,
                    ):
                        if old["normalized_content"] != normalized_content:
                            raise MemoryStoreError("conflict")
                        sources_to_insert = self._bind_prepared_grants(
                            conn,
                            grants=prepared_grants,
                            memory_id=int(old["id"]),
                            grant_kind=_GrantKind.EXPLICIT_USER_CORRECTION,
                        )
                        if requested_rank > existing_rank:
                            conn.execute(
                                """UPDATE memory_items
                                   SET sensitivity=?,last_confirmed_at=?,updated_at=?
                                   WHERE id=?""",
                                (sensitivity, stamp, stamp, int(old["id"])),
                            )
                        self._insert_sources(
                            conn, int(old["id"]), sources_to_insert, stamp,
                        )
                        old = conn.execute(
                            "SELECT * FROM memory_items WHERE id=?",
                            (int(old["id"]),),
                        ).fetchone()
                        conn.execute("COMMIT")
                        consumed = True
                        result = StoreResult("idempotent_noop", _safe_item(old))
                    else:
                        if (
                            sensitivity != "normal"
                            and not self._runtime_policy.sensitive_storage_enabled
                        ):
                            raise MemoryStoreError("sensitive_storage_disabled")
                        suppression_ids = self._matching_suppression_ids(
                            conn,
                            scope_type=old["scope_type"],
                            scope_ref=old["scope_ref"],
                            kind=old["kind"],
                            fingerprint=fingerprint,
                            fingerprint_version=self._runtime_policy.fingerprint_version,
                        )
                        if suppression_ids:
                            conn.execute("ROLLBACK")
                            consumed = True
                            result = StoreResult("suppressed")
                        else:
                            if self._find_live_by_fingerprint(
                                conn,
                                scope_type=old["scope_type"],
                                scope_ref=old["scope_ref"],
                                kind=old["kind"],
                                fingerprint=fingerprint,
                                fingerprint_version=self._runtime_policy.fingerprint_version,
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
                                    new_key,
                                    old["kind"],
                                    old["scope_type"],
                                    old["scope_ref"],
                                    normalized_content,
                                    fingerprint,
                                    self._runtime_policy.fingerprint_version,
                                    sensitivity,
                                    stamp,
                                    stamp,
                                    stamp,
                                    stamp,
                                ),
                            )
                            new_id = int(cursor.lastrowid)
                            sources_to_insert = self._bind_prepared_grants(
                                conn,
                                grants=prepared_grants,
                                memory_id=new_id,
                                grant_kind=_GrantKind.EXPLICIT_USER_CORRECTION,
                            )
                            self._insert_sources(conn, new_id, sources_to_insert, stamp)
                            updated = conn.execute(
                                """UPDATE memory_items
                                   SET status='superseded',superseded_by_id=?,updated_at=?
                                   WHERE id=? AND status='active'
                                     AND superseded_by_id IS NULL""",
                                (new_id, stamp, int(old["id"])),
                            )
                            if updated.rowcount != 1:
                                raise MemoryStoreError("conflict")
                            suppression_ids = (self._insert_suppression(
                                conn,
                                scope_type=old["scope_type"],
                                scope_ref=old["scope_ref"],
                                kind=old["kind"],
                                fingerprint=old_fingerprint,
                                fingerprint_version=int(old["fingerprint_version"]),
                                reason_category="corrected_obsolete",
                                stamp=stamp,
                            ),)
                            row = conn.execute(
                                "SELECT * FROM memory_items WHERE id=?", (new_id,),
                            ).fetchone()
                            conn.execute("COMMIT")
                            consumed = True
                            result = StoreResult("corrected", _safe_item(row))
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            return result
        except MemoryStoreError:
            raise
        except memory_action_ledger.MemoryActionLedgerError as error:
            raise MemoryStoreError(error.category) from None
        except memory_policy.MemoryPolicyError as error:
            raise MemoryStoreError(error.category) from None
        except (OSError, sqlite3.Error, ValueError) as error:
            raise self._translate_sqlite_error(error) from None
        finally:
            action_id = self._defer_action_to_transaction(
                _transaction,
                action_id,
                consumed=consumed,
                result=result,
                suppression_ids=suppression_ids,
            )
            self._finish_action(action_id, consumed=consumed)

    def forget_memory_atomic(
        self,
        *,
        memory_key: str,
        sources: Iterable[memory_policy.ProvenanceInput],
        authorization: object | None = None,
        _transaction: object | None = None,
    ) -> StoreResult:
        self._require_write_runtime()
        policy = self._trusted_policy()
        action_id: str | None = None
        consumed = False
        result: StoreResult | None = None
        suppression_ids: tuple[int, ...] = ()
        try:
            with self._write_connection(_transaction) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._require_write_runtime()
                    if authorization is None:
                        raise MemoryStoreError("authorization_required")
                    row = conn.execute(
                        f"""SELECT {_FORGET_RESULT_COLUMNS}
                            FROM memory_items WHERE memory_key=?""",
                        (memory_key,),
                    ).fetchone()
                    if row is None:
                        raise MemoryStoreError("not_found")
                    target = _forget_target_metadata(
                        row,
                        expected_memory_key=memory_key,
                    )
                    source, source_inputs = _binding_source(sources)
                    binding = memory_runtime.MemoryActionBinding(
                        action_type=memory_runtime.ACTION_FORGET_USER,
                        canonical_message_id=source.canonical_message_id,
                        kind=target.kind,
                        scope_type=target.scope_type,
                        scope_ref=target.scope_ref,
                        normalized_content=None,
                        sensitivity=target.sensitivity,
                        memory_key=memory_key,
                    )
                    action_id = self._begin_action(
                        authorization,
                        binding,
                        transaction=_transaction,
                    )
                    policy.validate_kind(target.kind)
                    policy.validate_scope(
                        target.scope_type,
                        target.scope_ref,
                    )
                    validated_inputs = policy.validate_provenance_inputs(
                        target.kind, source_inputs,
                    )
                    self._validate_or_initialize_profile(conn, initialize=True)
                    stamp = channel_store.now_iso()
                    prepared_grants = self._grant_action_sources(
                        conn,
                        sources=validated_inputs,
                        grant_kind=_GrantKind.EXPLICIT_USER_FORGET,
                        action_id=action_id,
                        stamp=stamp,
                    )
                    sources_to_insert = self._bind_prepared_grants(
                        conn,
                        grants=prepared_grants,
                        memory_id=target.memory_id,
                        grant_kind=_GrantKind.EXPLICIT_USER_FORGET,
                    )
                    self._insert_sources(
                        conn, target.memory_id, sources_to_insert, stamp,
                    )
                    if target.status == "forgotten":
                        suppression_ids = (
                            self._forgotten_target_suppression_ids(conn, row)
                        )
                        conn.execute("COMMIT")
                        consumed = True
                        result = StoreResult(
                            "already_forgotten",
                            _safe_forgotten_item(row, target),
                        )
                    else:
                        fingerprint = target.normalized_fingerprint
                        if type(fingerprint) is not bytes:
                            raise MemoryStoreError("invalid_state")
                        suppression_ids = (self._insert_suppression(
                            conn,
                            scope_type=target.scope_type,
                            scope_ref=target.scope_ref,
                            kind=target.kind,
                            fingerprint=fingerprint,
                            fingerprint_version=target.fingerprint_version,
                            reason_category="user_forget",
                            stamp=stamp,
                        ),)
                        updated = conn.execute(
                            """UPDATE memory_items
                               SET status='forgotten',normalized_content=NULL,
                                   normalized_fingerprint=NULL,
                                   superseded_by_id=NULL,updated_at=?
                               WHERE id=? AND status='active'""",
                            (stamp, target.memory_id),
                        )
                        if updated.rowcount != 1:
                            raise MemoryStoreError("conflict")
                        forgotten = conn.execute(
                            f"""SELECT {_FORGET_RESULT_COLUMNS}
                                FROM memory_items WHERE id=?""",
                            (target.memory_id,),
                        ).fetchone()
                        forgotten_target = _forget_target_metadata(
                            forgotten,
                            expected_memory_key=memory_key,
                        )
                        conn.execute("COMMIT")
                        consumed = True
                        result = StoreResult(
                            "forgotten",
                            _safe_forgotten_item(
                                forgotten,
                                forgotten_target,
                            ),
                        )
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            return result
        except MemoryStoreError:
            raise
        except memory_action_ledger.MemoryActionLedgerError as error:
            raise MemoryStoreError(error.category) from None
        except memory_policy.MemoryPolicyError as error:
            raise MemoryStoreError(error.category) from None
        except (OSError, sqlite3.Error, ValueError) as error:
            raise self._translate_sqlite_error(error) from None
        finally:
            action_id = self._defer_action_to_transaction(
                _transaction,
                action_id,
                consumed=consumed,
                result=result,
                suppression_ids=suppression_ids,
            )
            self._finish_action(action_id, consumed=consumed)

    def get_item_by_key(self, memory_key: str) -> dict | None:
        try:
            with channel_store.connect(self.path) as conn:
                row = conn.execute(
                    "SELECT * FROM memory_items WHERE memory_key=?",
                    (memory_key,),
                ).fetchone()
            return _safe_item(row) if row is not None else None
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None

    def get_sources(self, memory_key: str) -> list[dict]:
        try:
            with channel_store.connect(self.path) as conn:
                rows = conn.execute(
                    """SELECT s.channel,s.source,s.evidence_role,s.evidence_type,
                              s.created_at
                       FROM memory_sources s
                       JOIN memory_items m ON m.id=s.memory_id
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
        clauses.append(
            "sensitivity IN (%s)" % ",".join("?" for _ in sensitivities)
        )
        parameters.extend(sensitivities)
        parameters.append(limit)
        sql = (
            "SELECT * FROM memory_items WHERE "
            + " AND ".join(clauses)
            + " ORDER BY last_confirmed_at DESC,id DESC LIMIT ?"
        )
        try:
            with channel_store.connect(self.path) as conn:
                rows = conn.execute(sql, tuple(parameters)).fetchall()
            return [_safe_item(row) for row in rows]
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None


class MemoryReader:
    """Read-only query object with no Runtime Authority or fingerprint secret."""

    HARD_MAX_ITEMS = MemoryStore.HARD_MAX_ITEMS
    __slots__ = ("path", "_expected_profile")

    def __init__(
        self,
        path: str,
        *,
        expected_profile: tuple[str, bytes, int, int] | None,
    ):
        self.path = path
        self._expected_profile = expected_profile

    def __repr__(self) -> str:
        return "<MemoryReader>"

    validate_schema = MemoryStore.validate_schema
    get_item_by_key = MemoryStore.get_item_by_key
    get_sources = MemoryStore.get_sources
    get_active_items = MemoryStore.get_active_items

    def validate_runtime_profile_state(self) -> bool:
        expected = self._expected_profile
        if expected is None:
            raise MemoryStoreError("memory_fingerprint_profile_mismatch")
        key_id, key_check, normalization_version, fingerprint_version = expected
        try:
            with channel_store.connect(self.path) as conn:
                rows = conn.execute(
                    "SELECT * FROM memory_fingerprint_profile ORDER BY singleton"
                ).fetchall()
                if len(rows) > 1:
                    raise MemoryStoreError("memory_fingerprint_profile_mismatch")
                if not rows:
                    if MemoryStore._memory_state_count(conn):
                        raise MemoryStoreError(
                            "memory_fingerprint_profile_mismatch"
                        )
                    return True
                if not MemoryStore._profile_matches(
                    rows[0],
                    key_id=key_id,
                    key_check=key_check,
                    normalization_version=normalization_version,
                    fingerprint_version=fingerprint_version,
                ):
                    raise MemoryStoreError("memory_fingerprint_profile_mismatch")
                return True
        except MemoryStoreError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryStoreError("storage_unavailable") from None
