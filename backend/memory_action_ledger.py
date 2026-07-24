"""Internal request-ledger and transaction coordination for explicit Memory actions."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

try:
    from . import channel_store, memory_policy, memory_runtime
except ImportError:  # support direct module execution in local tooling
    import channel_store
    import memory_policy
    import memory_runtime


REQUEST_BINDING_DOMAIN = "memory-entry/request-binding/v1"
REQUEST_TERMINAL_DOMAIN = "memory-entry/request-terminal/v1"
CANONICAL_ACTION_CONTRACT_VERSION = 1
TERMINAL_SEMANTIC_SNAPSHOT_VERSION = 1
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
MEMORY_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
SCOPE_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ACTION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{24,96}\Z")
ACTION_KINDS = frozenset({"remember", "correct", "forget"})
ORIGINS = frozenset({"operator_cli", "mcp", "telegram", "operit"})
ORIGIN_CANONICAL_PROJECTION = {
    "operator_cli": ("web", "relay"),
    "mcp": ("relay", "mcp"),
    "telegram": ("telegram", "telegram"),
    "operit": ("operit_share", "operit"),
}
COMPLETED_CATEGORIES = {
    "remember": frozenset({"created", "idempotent_existing", "suppressed"}),
    "correct": frozenset({"corrected", "unchanged", "suppressed"}),
    "forget": frozenset({"forgotten", "already_forgotten"}),
}
FAILED_CATEGORIES = frozenset({
    "authorization_expired",
    "authorization_invalid",
    "authorization_not_yet_valid",
    "authorization_replayed",
    "conflict",
    "explicit_writes_disabled",
    "feature_disabled",
    "invalid_content",
    "invalid_kind",
    "invalid_memory_key",
    "invalid_provenance",
    "invalid_request",
    "invalid_scope",
    "invalid_sensitivity",
    "invalid_state",
    "memory_configuration_invalid",
    "memory_schema_invalid",
    "not_found",
    "request_binding_conflict",
    "sensitive_storage_disabled",
    "sensitivity_downgrade",
    "storage_unavailable",
    "terminal_semantics_invalid",
    "unsupported_evidence",
})

_UNIT_OF_WORK_TOKEN = object()


class MemoryActionLedgerError(RuntimeError):
    """A stable, data-free action-ledger failure."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True, repr=False)
class MemoryActionRequestBinding:
    request_id: str = field(repr=False)
    action_kind: str
    origin: str
    target_memory_key: str | None = field(default=None, repr=False)
    scope_type: str = ""
    scope_ref: str = field(default="", repr=False)
    kind: str = ""
    sensitivity: str = ""
    normalized_content: str | None = field(default=None, repr=False)
    normalization_version: int = memory_policy.NORMALIZATION_VERSION
    canonical_action_contract_version: int = CANONICAL_ACTION_CONTRACT_VERSION

    def __repr__(self) -> str:
        return "<MemoryActionRequestBinding>"


@dataclass(frozen=True, slots=True)
class MemoryActionLedgerResult:
    request_id: str
    action_kind: str
    status: str
    result_category: str
    result_memory_key: str | None


@dataclass(frozen=True, slots=True, repr=False)
class TerminalCanonicalSemanticV1:
    canonical_message_id: int
    direction: str
    kind: str
    channel: str
    source: str
    normalized_text: str
    metadata_projection: tuple[tuple[str, str], ...]
    canonical_action_contract_version: int


@dataclass(frozen=True, slots=True, repr=False)
class TerminalEvidenceSemanticV1:
    evidence_event_id: int
    canonical_message_id: int
    action_id: str
    action_type: str
    action_binding_version: int
    evidence_type: str
    reality_scope: str
    subject_scope: str
    created_by_component: str
    created_at: str


@dataclass(frozen=True, slots=True, repr=False)
class TerminalMemorySourceSemanticV1:
    source_id: int
    memory_id: int
    memory_key: str
    canonical_message_id: int
    evidence_event_id: int
    channel: str
    source: str
    evidence_role: str
    evidence_type: str
    created_at: str


@dataclass(frozen=True, slots=True, repr=False)
class TerminalMemoryItemSemanticV1:
    relation: str
    memory_id: int
    memory_key: str
    status: str
    kind: str
    scope_type: str
    scope_ref: str
    sensitivity: str
    content_present: bool
    normalized_content: str | None
    fingerprint_version: int
    normalized_fingerprint: str | None
    superseded_by_memory_key: str | None
    explicitness: str
    confidence: float


@dataclass(frozen=True, slots=True, repr=False)
class TerminalSuppressionSemanticV1:
    suppression_id: int
    relation: str
    scope_type: str
    scope_ref: str
    kind: str
    fingerprint_version: int
    normalized_fingerprint: str
    reason_category: str
    created_at: str


@dataclass(frozen=True, slots=True, repr=False)
class TerminalSemanticSnapshotV1:
    version: int
    canonical: TerminalCanonicalSemanticV1
    evidence: tuple[TerminalEvidenceSemanticV1, ...]
    sources: tuple[TerminalMemorySourceSemanticV1, ...]
    items: tuple[TerminalMemoryItemSemanticV1, ...]
    suppressions: tuple[TerminalSuppressionSemanticV1, ...]

    def __repr__(self) -> str:
        return "<TerminalSemanticSnapshotV1>"


def issue_request_id() -> str:
    """Issue an opaque server-side request identifier without external input."""
    return secrets.token_urlsafe(24)


def _valid_secret(secret: object) -> bool:
    if not (
        isinstance(secret, str)
        and 32 <= len(secret) <= 512
        and secret.isascii()
        and all(33 <= ord(char) <= 126 for char in secret)
    ):
        return False
    lowered = secret.casefold()
    if any(
        marker in lowered
        for marker in (
            "replace-with",
            "changeme",
            "placeholder",
            "example-secret",
        )
    ):
        return False
    character_classes = sum((
        any(char.islower() for char in secret),
        any(char.isupper() for char in secret),
        any(char.isdigit() for char in secret),
        any(not char.isalnum() for char in secret),
    ))
    return character_classes >= 3 and len(set(secret)) >= 16


def _validate_binding(binding: object) -> MemoryActionRequestBinding:
    if not isinstance(binding, MemoryActionRequestBinding):
        raise MemoryActionLedgerError("invalid_request")
    if (
        not isinstance(binding.request_id, str)
        or REQUEST_ID_PATTERN.fullmatch(binding.request_id) is None
        or binding.action_kind not in ACTION_KINDS
        or binding.origin not in ORIGINS
        or binding.scope_type not in memory_policy.SCOPE_TYPES
        or binding.kind not in memory_policy.KINDS
        or binding.kind == "assistant_experience"
        or binding.sensitivity not in memory_policy.SENSITIVITIES
        or type(binding.normalization_version) is not int
        or binding.normalization_version != memory_policy.NORMALIZATION_VERSION
        or type(binding.canonical_action_contract_version) is not int
        or binding.canonical_action_contract_version
        != CANONICAL_ACTION_CONTRACT_VERSION
    ):
        raise MemoryActionLedgerError("invalid_request")
    if binding.scope_type == "global_user":
        if binding.scope_ref != "":
            raise MemoryActionLedgerError("invalid_request")
    elif (
        not isinstance(binding.scope_ref, str)
        or SCOPE_REF_PATTERN.fullmatch(binding.scope_ref) is None
        or (
            binding.scope_type == "channel"
            and binding.scope_ref not in memory_policy.KNOWN_CHANNELS
        )
    ):
        raise MemoryActionLedgerError("invalid_request")
    if binding.action_kind == "remember":
        if binding.target_memory_key is not None:
            raise MemoryActionLedgerError("invalid_request")
    elif (
        not isinstance(binding.target_memory_key, str)
        or MEMORY_KEY_PATTERN.fullmatch(binding.target_memory_key) is None
    ):
        raise MemoryActionLedgerError("invalid_request")
    content = binding.normalized_content
    if binding.action_kind in {"remember", "correct"}:
        if (
            not isinstance(content, str)
            or not content
            or len(content) > 4096
        ):
            raise MemoryActionLedgerError("invalid_request")
        try:
            if memory_policy.normalize_content(content, max_chars=4096) != content:
                raise MemoryActionLedgerError("invalid_request")
        except memory_policy.MemoryPolicyError:
            raise MemoryActionLedgerError("invalid_request") from None
    elif content is not None and (
        not isinstance(content, str) or not content or len(content) > 4096
    ):
        raise MemoryActionLedgerError("invalid_request")
    return binding


def request_binding_digest(
    secret: str,
    binding: MemoryActionRequestBinding,
) -> bytes:
    """Bind a request with domain-separated HMAC-SHA-256; never persist content."""
    if not _valid_secret(secret):
        raise MemoryActionLedgerError("memory_configuration_invalid")
    validated = _validate_binding(binding)
    try:
        payload = _encode_binding_payload(
            validated,
            domain=REQUEST_BINDING_DOMAIN,
        )
    except (TypeError, ValueError, UnicodeError):
        raise MemoryActionLedgerError("invalid_request") from None
    return hmac.new(secret.encode("ascii"), payload, hashlib.sha256).digest()


def _encode_binding_payload(
    binding: MemoryActionRequestBinding,
    *,
    domain: str,
    terminal: dict[str, object] | None = None,
) -> bytes:
    payload: dict[str, object] = {
        "action_kind": binding.action_kind,
        "canonical_action_contract_version":
            binding.canonical_action_contract_version,
        "domain": domain,
        "kind": binding.kind,
        "normalization_version": binding.normalization_version,
        "normalized_content": binding.normalized_content,
        "origin": binding.origin,
        "request_id": binding.request_id,
        "scope_ref": binding.scope_ref,
        "scope_type": binding.scope_type,
        "sensitivity": binding.sensitivity,
        "target_memory_key": binding.target_memory_key,
    }
    if terminal is not None:
        payload["terminal"] = terminal
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _terminal_binding_digest(
    secret: str,
    binding: MemoryActionRequestBinding,
    *,
    canonical_message_id: int | None,
    status: str,
    result_category: str,
    result_memory_key: str | None,
    semantic_snapshot: TerminalSemanticSnapshotV1 | None,
) -> bytes:
    if not _valid_secret(secret):
        raise MemoryActionLedgerError("memory_configuration_invalid")
    validated = _validate_binding(binding)
    if (
        status == "completed"
        and (
            type(canonical_message_id) is not int
            or canonical_message_id <= 0
            or result_category not in COMPLETED_CATEGORIES[
                validated.action_kind
            ]
            or not isinstance(
                semantic_snapshot,
                TerminalSemanticSnapshotV1,
            )
            or semantic_snapshot.version
            != TERMINAL_SEMANTIC_SNAPSHOT_VERSION
        )
    ):
        raise MemoryActionLedgerError("invalid_request")
    if (
        status == "failed"
        and (
            canonical_message_id is not None
            or result_memory_key is not None
            or result_category not in FAILED_CATEGORIES
            or semantic_snapshot is not None
        )
    ):
        raise MemoryActionLedgerError("invalid_request")
    if status not in {"completed", "failed"}:
        raise MemoryActionLedgerError("invalid_request")
    try:
        payload = _encode_binding_payload(
            validated,
            domain=REQUEST_TERMINAL_DOMAIN,
            terminal={
                "canonical_message_id": canonical_message_id,
                "result_category": result_category,
                "result_memory_key": result_memory_key,
                "semantic_snapshot": (
                    asdict(semantic_snapshot)
                    if semantic_snapshot is not None
                    else None
                ),
                "status": status,
            },
        )
    except (TypeError, ValueError, UnicodeError):
        raise MemoryActionLedgerError("invalid_request") from None
    return hmac.new(secret.encode("ascii"), payload, hashlib.sha256).digest()


def _safe_ledger_result(row: sqlite3.Row) -> MemoryActionLedgerResult:
    try:
        request_id = row["request_id"]
        action_kind = row["action_kind"]
        status = row["status"]
        result_category = row["result_category"]
        result_memory_key = row["result_memory_key"]
        digest = row["request_binding_digest"]
    except (IndexError, KeyError):
        raise MemoryActionLedgerError("memory_schema_invalid") from None
    if (
        not isinstance(request_id, str)
        or REQUEST_ID_PATTERN.fullmatch(request_id) is None
        or action_kind not in ACTION_KINDS
        or status not in {"completed", "failed"}
        or not isinstance(result_category, str)
        or not isinstance(digest, bytes)
        or len(digest) != 32
        or (
            result_memory_key is not None
            and (
                not isinstance(result_memory_key, str)
                or MEMORY_KEY_PATTERN.fullmatch(result_memory_key) is None
            )
        )
    ):
        raise MemoryActionLedgerError("memory_schema_invalid")
    if (
        status == "completed"
        and result_category not in COMPLETED_CATEGORIES[action_kind]
    ) or (
        status == "failed"
        and result_category not in FAILED_CATEGORIES
    ):
        raise MemoryActionLedgerError("memory_schema_invalid")
    return MemoryActionLedgerResult(
        request_id=request_id,
        action_kind=action_kind,
        status=status,
        result_category=result_category,
        result_memory_key=result_memory_key,
    )


class _CoordinatedStoreConnection:
    """Translate Store transaction boundaries into one root UoW savepoint."""

    __slots__ = ("_uow", "_connection", "_active")

    def __init__(self, uow: "_MemoryActionUnitOfWork"):
        self._uow = uow
        self._connection = uow._connection
        self._active = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._active:
            self._rollback_savepoint()
        if exc_type is not None:
            self._uow._store_failed = True
        return False

    @property
    def in_transaction(self) -> bool:
        return self._active

    def _rollback_savepoint(self) -> None:
        self._connection.execute("ROLLBACK TO SAVEPOINT memory_store_action")
        self._connection.execute("RELEASE SAVEPOINT memory_store_action")
        self._active = False

    def execute(self, sql: str, parameters: Any = ()):
        statement = " ".join(str(sql).strip().upper().split())
        if statement == "BEGIN IMMEDIATE":
            if self._active:
                raise sqlite3.OperationalError("nested_memory_store_transaction")
            self._connection.execute("SAVEPOINT memory_store_action")
            self._active = True
            return self
        if statement == "COMMIT":
            if not self._active:
                raise sqlite3.OperationalError("memory_store_transaction_missing")
            result = self._connection.execute("RELEASE SAVEPOINT memory_store_action")
            self._active = False
            self._uow._store_completed = True
            return result
        if statement == "ROLLBACK":
            if not self._active:
                raise sqlite3.OperationalError("memory_store_transaction_missing")
            self._rollback_savepoint()
            self._uow._store_completed = True
            return self
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


class _MemoryActionUnitOfWork:
    """Trusted-process internal UoW; not an adapter or sandbox boundary."""

    __slots__ = (
        "_store",
        "_secret",
        "_connection",
        "_binding",
        "_binding_digest",
        "_canonical_message_id",
        "_terminal",
        "_replay",
        "_deferred_actions",
        "_store_completed",
        "_store_failed",
        "_entered",
        "_closed",
    )

    def __init__(
        self,
        token: object,
        *,
        store: object,
        secret: str,
    ):
        if token is not _UNIT_OF_WORK_TOKEN or not _valid_secret(secret):
            raise MemoryActionLedgerError("memory_configuration_invalid")
        self._store = store
        self._secret = secret
        self._connection: sqlite3.Connection | None = None
        self._binding: MemoryActionRequestBinding | None = None
        self._binding_digest: bytes | None = None
        self._canonical_message_id: int | None = None
        self._terminal: MemoryActionLedgerResult | None = None
        self._replay: MemoryActionLedgerResult | None = None
        self._deferred_actions: list[str] = []
        self._store_completed = False
        self._store_failed = False
        self._entered = False
        self._closed = False

    def __repr__(self) -> str:
        return "<MemoryActionUnitOfWork>"

    def __enter__(self):
        if self._entered or self._closed:
            raise MemoryActionLedgerError("invalid_state")
        self._connection = channel_store.connect(self._store.path)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except (OSError, sqlite3.Error, ValueError):
            self._connection.close()
            self._connection = None
            raise MemoryActionLedgerError("storage_unavailable") from None
        try:
            channel_store.validate_memory_action_schema(self._connection)
        except (OSError, sqlite3.Error, ValueError):
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            self._connection.close()
            self._connection = None
            raise MemoryActionLedgerError("memory_schema_invalid") from None
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._closed:
            self.rollback()
        return False

    def _require_active(self) -> sqlite3.Connection:
        if (
            not self._entered
            or self._closed
            or self._connection is None
            or not self._connection.in_transaction
        ):
            raise MemoryActionLedgerError("invalid_state")
        return self._connection

    def _execute(self, sql: str, parameters: Any = ()):
        conn = self._require_active()
        try:
            return conn.execute(sql, parameters)
        except sqlite3.IntegrityError:
            raise MemoryActionLedgerError("conflict") from None
        except (OSError, sqlite3.Error, ValueError):
            raise MemoryActionLedgerError("storage_unavailable") from None

    @staticmethod
    def _semantic_error() -> MemoryActionLedgerError:
        return MemoryActionLedgerError("terminal_semantics_invalid")

    def _canonical_semantic(
        self,
        canonical_message_id: int,
    ) -> TerminalCanonicalSemanticV1:
        row = self._execute(
            """SELECT id,direction,kind,text,meta FROM messages WHERE id=?""",
            (canonical_message_id,),
        ).fetchone()
        if row is None:
            raise self._semantic_error()
        try:
            raw_text = row["text"]
            raw_meta = row["meta"]
            if (
                type(row["id"]) is not int
                or int(row["id"]) != canonical_message_id
                or not isinstance(row["direction"], str)
                or not isinstance(row["kind"], str)
                or not isinstance(raw_text, str)
                or not raw_text
                or len(raw_text) > 4096
                or not isinstance(raw_meta, str)
                or len(raw_meta.encode("utf-8")) > 16 * 1024
            ):
                raise self._semantic_error()
            normalized_text = memory_policy.normalize_content(
                raw_text,
                max_chars=4096,
            )
            if normalized_text != raw_text:
                raise self._semantic_error()
            metadata = json.loads(raw_meta)
            if (
                not isinstance(metadata, dict)
                or set(metadata) != {"channel", "source"}
                or not isinstance(metadata["channel"], str)
                or not isinstance(metadata["source"], str)
            ):
                raise self._semantic_error()
            channel = metadata["channel"]
            source = metadata["source"]
            if (
                channel not in memory_policy.KNOWN_CHANNELS
                or SCOPE_REF_PATTERN.fullmatch(channel) is None
                or (
                    source
                    and SCOPE_REF_PATTERN.fullmatch(source) is None
                )
            ):
                raise self._semantic_error()
        except MemoryActionLedgerError:
            raise
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            memory_policy.MemoryPolicyError,
        ):
            raise self._semantic_error() from None
        return TerminalCanonicalSemanticV1(
            canonical_message_id=canonical_message_id,
            direction=row["direction"],
            kind=row["kind"],
            channel=channel,
            source=source,
            normalized_text=normalized_text,
            metadata_projection=(
                ("channel", channel),
                ("source", source),
            ),
            canonical_action_contract_version=(
                CANONICAL_ACTION_CONTRACT_VERSION
            ),
        )

    def _item_semantic(
        self,
        memory_key: str,
        *,
        relation: str,
    ) -> TerminalMemoryItemSemanticV1:
        row = self._execute(
            """SELECT i.*,s.memory_key AS superseded_by_memory_key
               FROM memory_items i
               LEFT JOIN memory_items s ON s.id=i.superseded_by_id
               WHERE i.memory_key=?""",
            (memory_key,),
        ).fetchone()
        if row is None:
            raise self._semantic_error()
        try:
            fingerprint = row["normalized_fingerprint"]
            content = row["normalized_content"]
            confidence = row["confidence"]
            superseded_key = row["superseded_by_memory_key"]
            if (
                type(row["id"]) is not int
                or int(row["id"]) <= 0
                or not isinstance(row["memory_key"], str)
                or MEMORY_KEY_PATTERN.fullmatch(row["memory_key"]) is None
                or row["memory_key"] != memory_key
                or row["status"] not in {
                    "candidate",
                    "active",
                    "superseded",
                    "forgotten",
                    "rejected",
                }
                or row["kind"] not in memory_policy.KINDS
                or row["scope_type"] not in memory_policy.SCOPE_TYPES
                or not isinstance(row["scope_ref"], str)
                or row["sensitivity"] not in memory_policy.SENSITIVITIES
                or type(row["fingerprint_version"]) is not int
                or int(row["fingerprint_version"]) <= 0
                or row["explicitness"] not in {"explicit", "inferred"}
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0.0 <= float(confidence) <= 1.0
                or (
                    superseded_key is not None
                    and (
                        not isinstance(superseded_key, str)
                        or MEMORY_KEY_PATTERN.fullmatch(superseded_key)
                        is None
                    )
                )
            ):
                raise self._semantic_error()
            if row["scope_type"] == "global_user":
                if row["scope_ref"] != "":
                    raise self._semantic_error()
            elif (
                SCOPE_REF_PATTERN.fullmatch(row["scope_ref"]) is None
                or (
                    row["scope_type"] == "channel"
                    and row["scope_ref"] not in memory_policy.KNOWN_CHANNELS
                )
            ):
                raise self._semantic_error()
            content_present = content is not None
            if content_present:
                if (
                    not isinstance(content, str)
                    or not content
                    or len(content) > 4096
                    or memory_policy.normalize_content(
                        content,
                        max_chars=4096,
                    )
                    != content
                    or not isinstance(fingerprint, bytes)
                    or len(fingerprint) != 32
                ):
                    raise self._semantic_error()
            elif fingerprint is not None:
                raise self._semantic_error()
        except MemoryActionLedgerError:
            raise
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            memory_policy.MemoryPolicyError,
        ):
            raise self._semantic_error() from None
        return TerminalMemoryItemSemanticV1(
            relation=relation,
            memory_id=int(row["id"]),
            memory_key=memory_key,
            status=row["status"],
            kind=row["kind"],
            scope_type=row["scope_type"],
            scope_ref=row["scope_ref"],
            sensitivity=row["sensitivity"],
            content_present=content_present,
            normalized_content=content if content_present else None,
            fingerprint_version=int(row["fingerprint_version"]),
            normalized_fingerprint=(
                fingerprint.hex()
                if isinstance(fingerprint, bytes)
                else None
            ),
            superseded_by_memory_key=superseded_key,
            explicitness=row["explicitness"],
            confidence=float(confidence),
        )

    def _source_semantics(
        self,
        *,
        canonical_message_id: int,
        item_ids: tuple[int, ...],
    ) -> tuple[TerminalMemorySourceSemanticV1, ...]:
        clauses = ["ms.canonical_message_id=?"]
        parameters: list[object] = [canonical_message_id]
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            clauses.append(f"ms.memory_id IN ({placeholders})")
            parameters.extend(item_ids)
        rows = self._execute(
            f"""SELECT ms.*,mi.memory_key
                FROM memory_sources ms
                JOIN memory_items mi ON mi.id=ms.memory_id
                WHERE {' OR '.join(clauses)}
                ORDER BY ms.id""",
            tuple(parameters),
        ).fetchall()
        result: list[TerminalMemorySourceSemanticV1] = []
        for row in rows:
            try:
                if (
                    type(row["id"]) is not int
                    or int(row["id"]) <= 0
                    or type(row["memory_id"]) is not int
                    or int(row["memory_id"]) <= 0
                    or not isinstance(row["memory_key"], str)
                    or MEMORY_KEY_PATTERN.fullmatch(row["memory_key"]) is None
                    or type(row["canonical_message_id"]) is not int
                    or int(row["canonical_message_id"]) <= 0
                    or type(row["evidence_event_id"]) is not int
                    or int(row["evidence_event_id"]) <= 0
                    or not isinstance(row["channel"], str)
                    or row["channel"] not in memory_policy.KNOWN_CHANNELS
                    or not isinstance(row["source"], str)
                    or (
                        row["source"]
                        and SCOPE_REF_PATTERN.fullmatch(row["source"]) is None
                    )
                    or row["evidence_role"] not in {"user", "assistant"}
                    or row["evidence_type"]
                    not in memory_policy.ALL_EVIDENCE_TYPES
                    or not isinstance(row["created_at"], str)
                ):
                    raise self._semantic_error()
            except MemoryActionLedgerError:
                raise
            except (AttributeError, KeyError, TypeError, ValueError):
                raise self._semantic_error() from None
            result.append(TerminalMemorySourceSemanticV1(
                source_id=int(row["id"]),
                memory_id=int(row["memory_id"]),
                memory_key=row["memory_key"],
                canonical_message_id=int(row["canonical_message_id"]),
                evidence_event_id=int(row["evidence_event_id"]),
                channel=row["channel"],
                source=row["source"],
                evidence_role=row["evidence_role"],
                evidence_type=row["evidence_type"],
                created_at=row["created_at"],
            ))
        return tuple(result)

    def _evidence_semantics(
        self,
        *,
        canonical_message_id: int,
        evidence_ids: tuple[int, ...],
    ) -> tuple[TerminalEvidenceSemanticV1, ...]:
        clauses = ["canonical_message_id=?"]
        parameters: list[object] = [canonical_message_id]
        if evidence_ids:
            placeholders = ",".join("?" for _ in evidence_ids)
            clauses.append(f"id IN ({placeholders})")
            parameters.extend(evidence_ids)
        rows = self._execute(
            f"""SELECT * FROM memory_evidence_events
                WHERE {' OR '.join(clauses)}
                ORDER BY id""",
            tuple(parameters),
        ).fetchall()
        result: list[TerminalEvidenceSemanticV1] = []
        for row in rows:
            try:
                if (
                    type(row["id"]) is not int
                    or int(row["id"]) <= 0
                    or type(row["canonical_message_id"]) is not int
                    or int(row["canonical_message_id"]) <= 0
                    or not isinstance(row["action_id"], str)
                    or ACTION_ID_PATTERN.fullmatch(row["action_id"]) is None
                    or row["action_type"] not in memory_runtime.ACTION_TYPES
                    or type(row["action_binding_version"]) is not int
                    or int(row["action_binding_version"])
                    != memory_runtime.ACTION_BINDING_VERSION
                    or row["evidence_type"]
                    not in memory_policy.ALL_EVIDENCE_TYPES
                    or row["reality_scope"]
                    not in memory_policy.REALITY_SCOPES
                    or row["subject_scope"]
                    not in memory_policy.SUBJECT_SCOPES
                    or row["created_by_component"]
                    not in memory_policy.EVIDENCE_COMPONENTS
                    or not isinstance(row["created_at"], str)
                ):
                    raise self._semantic_error()
            except MemoryActionLedgerError:
                raise
            except (AttributeError, KeyError, TypeError, ValueError):
                raise self._semantic_error() from None
            result.append(TerminalEvidenceSemanticV1(
                evidence_event_id=int(row["id"]),
                canonical_message_id=int(row["canonical_message_id"]),
                action_id=row["action_id"],
                action_type=row["action_type"],
                action_binding_version=int(row["action_binding_version"]),
                evidence_type=row["evidence_type"],
                reality_scope=row["reality_scope"],
                subject_scope=row["subject_scope"],
                created_by_component=row["created_by_component"],
                created_at=row["created_at"],
            ))
        return tuple(result)

    def _suppression_semantics(
        self,
        *,
        binding: MemoryActionRequestBinding,
        relation: str,
        normalized_content: str | None,
        fingerprint_version: int,
        reason_category: str | None,
    ) -> tuple[TerminalSuppressionSemanticV1, ...]:
        if normalized_content is None:
            return ()
        try:
            fingerprint = memory_policy.fingerprint_content(
                self._secret,
                scope_type=binding.scope_type,
                scope_ref=binding.scope_ref,
                kind=binding.kind,
                normalized_content=normalized_content,
            )
        except memory_policy.MemoryPolicyError:
            raise self._semantic_error() from None
        sql = """SELECT * FROM memory_suppressions
                 WHERE scope_type=? AND scope_ref=? AND kind=?
                   AND fingerprint_version=?
                   AND normalized_fingerprint=?"""
        parameters: list[object] = [
            binding.scope_type,
            binding.scope_ref,
            binding.kind,
            fingerprint_version,
            fingerprint,
        ]
        if reason_category is not None:
            sql += " AND reason_category=?"
            parameters.append(reason_category)
        sql += " ORDER BY id"
        rows = self._execute(sql, tuple(parameters)).fetchall()
        result: list[TerminalSuppressionSemanticV1] = []
        for row in rows:
            stored = row["normalized_fingerprint"]
            if (
                type(row["id"]) is not int
                or int(row["id"]) <= 0
                or row["scope_type"] != binding.scope_type
                or row["scope_ref"] != binding.scope_ref
                or row["kind"] != binding.kind
                or type(row["fingerprint_version"]) is not int
                or int(row["fingerprint_version"]) != fingerprint_version
                or not isinstance(stored, bytes)
                or len(stored) != 32
                or not hmac.compare_digest(stored, fingerprint)
                or row["reason_category"]
                not in {
                    "user_forget",
                    "user_reject",
                    "privacy_policy",
                    "corrected_obsolete",
                }
                or not isinstance(row["created_at"], str)
            ):
                raise self._semantic_error()
            result.append(TerminalSuppressionSemanticV1(
                suppression_id=int(row["id"]),
                relation=relation,
                scope_type=row["scope_type"],
                scope_ref=row["scope_ref"],
                kind=row["kind"],
                fingerprint_version=int(row["fingerprint_version"]),
                normalized_fingerprint=stored.hex(),
                reason_category=row["reason_category"],
                created_at=row["created_at"],
            ))
        return tuple(result)

    def _build_terminal_semantic_snapshot(
        self,
        binding: MemoryActionRequestBinding,
        *,
        canonical_message_id: int,
        result_category: str,
        result_memory_key: str | None,
    ) -> TerminalSemanticSnapshotV1:
        canonical = self._canonical_semantic(canonical_message_id)
        items: list[TerminalMemoryItemSemanticV1] = []
        target_key = binding.target_memory_key
        if target_key is not None:
            relation = (
                "target_result"
                if target_key == result_memory_key
                else "target"
            )
            items.append(self._item_semantic(target_key, relation=relation))
        if (
            result_memory_key is not None
            and result_memory_key != target_key
        ):
            items.append(self._item_semantic(
                result_memory_key,
                relation="result",
            ))
        item_ids = tuple(item.memory_id for item in items)
        sources = self._source_semantics(
            canonical_message_id=canonical_message_id,
            item_ids=item_ids,
        )
        evidence = self._evidence_semantics(
            canonical_message_id=canonical_message_id,
            evidence_ids=tuple(
                source.evidence_event_id for source in sources
            ),
        )
        suppressions: tuple[TerminalSuppressionSemanticV1, ...] = ()
        fingerprint_version = memory_policy.FINGERPRINT_VERSION
        if result_category == "suppressed":
            suppressions = self._suppression_semantics(
                binding=binding,
                relation="request_content",
                normalized_content=binding.normalized_content,
                fingerprint_version=fingerprint_version,
                reason_category=None,
            )
        elif (
            binding.action_kind == "forget"
            and result_category == "forgotten"
        ):
            suppressions = self._suppression_semantics(
                binding=binding,
                relation="forgotten_target",
                normalized_content=binding.normalized_content,
                fingerprint_version=fingerprint_version,
                reason_category="user_forget",
            )
        elif (
            binding.action_kind == "correct"
            and result_category == "corrected"
        ):
            target = next(
                (item for item in items if item.relation == "target"),
                None,
            )
            if (
                target is None
                or not target.content_present
                or target.normalized_content is None
            ):
                raise self._semantic_error()
            suppressions = self._suppression_semantics(
                binding=binding,
                relation="superseded_target",
                normalized_content=target.normalized_content,
                fingerprint_version=target.fingerprint_version,
                reason_category="corrected_obsolete",
            )
        return TerminalSemanticSnapshotV1(
            version=TERMINAL_SEMANTIC_SNAPSHOT_VERSION,
            canonical=canonical,
            evidence=evidence,
            sources=sources,
            items=tuple(items),
            suppressions=suppressions,
        )

    @staticmethod
    def _expected_evidence_semantics(
        binding: MemoryActionRequestBinding,
    ) -> tuple[str, str, str, str, str]:
        if binding.action_kind == "remember":
            if binding.kind == "decision":
                return (
                    memory_runtime.ACTION_CONFIRM_DECISION,
                    "confirmed_project_decision",
                    "real",
                    "project",
                    "memory_admin",
                )
            return (
                memory_runtime.ACTION_REMEMBER_USER,
                "explicit_user_memory",
                "real",
                "user",
                "memory_admin",
            )
        if binding.action_kind == "correct":
            return (
                memory_runtime.ACTION_CORRECT_USER,
                "explicit_user_correction",
                "real",
                "user",
                "memory_admin",
            )
        return (
            memory_runtime.ACTION_FORGET_USER,
            "user_forget",
            "real",
            "user",
            "memory_admin",
        )

    def _validate_terminal_semantic_snapshot(
        self,
        binding: MemoryActionRequestBinding,
        snapshot: TerminalSemanticSnapshotV1,
        *,
        canonical_message_id: int,
        result_category: str,
        result_memory_key: str | None,
        expected_action_id: str | None,
    ) -> None:
        if (
            not isinstance(snapshot, TerminalSemanticSnapshotV1)
            or snapshot.version != TERMINAL_SEMANTIC_SNAPSHOT_VERSION
            or snapshot.canonical.canonical_message_id
            != canonical_message_id
            or snapshot.canonical.direction != "in"
            or snapshot.canonical.kind != "user"
            or snapshot.canonical.canonical_action_contract_version
            != binding.canonical_action_contract_version
        ):
            raise self._semantic_error()
        expected_channel, expected_source = ORIGIN_CANONICAL_PROJECTION[
            binding.origin
        ]
        if (
            snapshot.canonical.channel != expected_channel
            or snapshot.canonical.source != expected_source
            or snapshot.canonical.metadata_projection
            != (
                ("channel", expected_channel),
                ("source", expected_source),
            )
            or (
                binding.normalized_content is not None
                and snapshot.canonical.normalized_text
                != binding.normalized_content
            )
        ):
            raise self._semantic_error()

        items_by_relation = {
            item.relation: item for item in snapshot.items
        }
        if (
            len(items_by_relation) != len(snapshot.items)
            or len({item.memory_id for item in snapshot.items})
            != len(snapshot.items)
            or len({item.memory_key for item in snapshot.items})
            != len(snapshot.items)
        ):
            raise self._semantic_error()
        for item in snapshot.items:
            if (
                item.explicitness != "explicit"
                or item.confidence != 1.0
                or item.fingerprint_version
                != memory_policy.FINGERPRINT_VERSION
            ):
                raise self._semantic_error()
            if item.content_present:
                try:
                    expected_fingerprint = memory_policy.fingerprint_content(
                        self._secret,
                        scope_type=item.scope_type,
                        scope_ref=item.scope_ref,
                        kind=item.kind,
                        normalized_content=item.normalized_content,
                    ).hex()
                except memory_policy.MemoryPolicyError:
                    raise self._semantic_error() from None
                if item.normalized_fingerprint != expected_fingerprint:
                    raise self._semantic_error()
            elif (
                item.normalized_content is not None
                or item.normalized_fingerprint is not None
            ):
                raise self._semantic_error()

        sensitivity_rank = {
            "normal": 0,
            "sensitive": 1,
            "restricted": 2,
        }

        def require_binding_fields(
            item: TerminalMemoryItemSemanticV1,
            *,
            require_content: bool,
            sensitivity_mode: str,
        ) -> None:
            item_rank = sensitivity_rank[item.sensitivity]
            binding_rank = sensitivity_rank[binding.sensitivity]
            if (
                item.kind != binding.kind
                or item.scope_type != binding.scope_type
                or item.scope_ref != binding.scope_ref
                or (
                    require_content
                    and (
                        not item.content_present
                        or item.normalized_content
                        != binding.normalized_content
                    )
                )
                or (
                    sensitivity_mode == "exact"
                    and item.sensitivity != binding.sensitivity
                )
                or (
                    sensitivity_mode == "at_least"
                    and item_rank < binding_rank
                )
                or (
                    sensitivity_mode == "at_most"
                    and item_rank > binding_rank
                )
            ):
                raise self._semantic_error()

        action_item: TerminalMemoryItemSemanticV1 | None = None
        if binding.action_kind == "remember":
            if result_category == "suppressed":
                if snapshot.items or result_memory_key is not None:
                    raise self._semantic_error()
            else:
                result_item = items_by_relation.get("result")
                if (
                    len(snapshot.items) != 1
                    or result_item is None
                    or result_item.memory_key != result_memory_key
                    or result_item.status != "active"
                    or result_item.superseded_by_memory_key is not None
                ):
                    raise self._semantic_error()
                require_binding_fields(
                    result_item,
                    require_content=True,
                    sensitivity_mode=(
                        "exact"
                        if result_category == "created"
                        else "at_least"
                    ),
                )
                action_item = result_item
        elif binding.action_kind == "correct":
            target_key = binding.target_memory_key
            if result_category == "corrected":
                target = items_by_relation.get("target")
                result_item = items_by_relation.get("result")
                if (
                    len(snapshot.items) != 2
                    or target is None
                    or result_item is None
                    or target.memory_key != target_key
                    or result_item.memory_key != result_memory_key
                    or target.memory_key == result_item.memory_key
                    or target.status != "superseded"
                    or target.superseded_by_memory_key
                    != result_item.memory_key
                    or result_item.status != "active"
                    or result_item.superseded_by_memory_key is not None
                ):
                    raise self._semantic_error()
                require_binding_fields(
                    target,
                    require_content=False,
                    sensitivity_mode="at_most",
                )
                require_binding_fields(
                    result_item,
                    require_content=True,
                    sensitivity_mode="exact",
                )
                if target.normalized_content == binding.normalized_content:
                    raise self._semantic_error()
                action_item = result_item
            elif result_category == "unchanged":
                target = items_by_relation.get("target_result")
                if (
                    len(snapshot.items) != 1
                    or target is None
                    or target.memory_key != target_key
                    or result_memory_key != target_key
                    or target.status != "active"
                    or target.superseded_by_memory_key is not None
                ):
                    raise self._semantic_error()
                require_binding_fields(
                    target,
                    require_content=True,
                    sensitivity_mode="exact",
                )
                action_item = target
            else:
                target = items_by_relation.get("target")
                if (
                    result_category != "suppressed"
                    or len(snapshot.items) != 1
                    or target is None
                    or target.memory_key != target_key
                    or result_memory_key is not None
                    or target.status != "active"
                ):
                    raise self._semantic_error()
                require_binding_fields(
                    target,
                    require_content=False,
                    sensitivity_mode="at_most",
                )
                if target.normalized_content == binding.normalized_content:
                    raise self._semantic_error()
        else:
            target = items_by_relation.get("target_result")
            if (
                len(snapshot.items) != 1
                or target is None
                or target.memory_key != binding.target_memory_key
                or result_memory_key != binding.target_memory_key
                or target.status != "forgotten"
                or target.content_present
                or target.superseded_by_memory_key is not None
            ):
                raise self._semantic_error()
            require_binding_fields(
                target,
                require_content=False,
                sensitivity_mode="exact",
            )
            action_item = target

        expected_action_type, expected_evidence_type, reality, subject, component = (
            self._expected_evidence_semantics(binding)
        )
        evidence_by_id = {
            evidence.evidence_event_id: evidence
            for evidence in snapshot.evidence
        }
        if len(evidence_by_id) != len(snapshot.evidence):
            raise self._semantic_error()
        current_evidence = tuple(
            evidence
            for evidence in snapshot.evidence
            if evidence.canonical_message_id == canonical_message_id
        )
        current_sources = tuple(
            source
            for source in snapshot.sources
            if source.canonical_message_id == canonical_message_id
        )
        if result_category == "suppressed":
            if current_evidence or current_sources:
                raise self._semantic_error()
        else:
            if len(current_evidence) != 1 or len(current_sources) != 1:
                raise self._semantic_error()
            evidence = current_evidence[0]
            source = current_sources[0]
            if (
                evidence.action_type != expected_action_type
                or evidence.evidence_type != expected_evidence_type
                or evidence.reality_scope != reality
                or evidence.subject_scope != subject
                or evidence.created_by_component != component
                or evidence.action_binding_version
                != memory_runtime.ACTION_BINDING_VERSION
                or (
                    expected_action_id is not None
                    and evidence.action_id != expected_action_id
                )
                or source.evidence_event_id
                != evidence.evidence_event_id
                or source.evidence_type != expected_evidence_type
                or source.evidence_role != "user"
                or source.channel != snapshot.canonical.channel
                or source.source != snapshot.canonical.source
                or action_item is None
                or source.memory_id != action_item.memory_id
                or source.memory_key != action_item.memory_key
            ):
                raise self._semantic_error()

        item_ids = {item.memory_id for item in snapshot.items}
        source_ids = set()
        for source in snapshot.sources:
            evidence = evidence_by_id.get(source.evidence_event_id)
            if (
                source.source_id in source_ids
                or source.memory_id not in item_ids
                or evidence is None
                or evidence.canonical_message_id
                != source.canonical_message_id
                or evidence.evidence_type != source.evidence_type
            ):
                raise self._semantic_error()
            source_ids.add(source.source_id)
        if {
            evidence.evidence_event_id for evidence in snapshot.evidence
        } != {source.evidence_event_id for source in snapshot.sources} | {
            evidence.evidence_event_id for evidence in current_evidence
        }:
            raise self._semantic_error()

        if (
            binding.action_kind == "remember"
            and result_category == "created"
            and len(snapshot.sources) != 1
        ) or (
            binding.action_kind == "correct"
            and result_category == "corrected"
            and len(tuple(
                source
                for source in snapshot.sources
                if source.memory_key == result_memory_key
            ))
            != 1
        ):
            raise self._semantic_error()

        needs_suppression = (
            result_category == "suppressed"
            or (
                binding.action_kind == "correct"
                and result_category == "corrected"
            )
            or (
                binding.action_kind == "forget"
                and result_category == "forgotten"
            )
        )
        if (
            needs_suppression and len(snapshot.suppressions) != 1
        ) or (
            not needs_suppression and snapshot.suppressions
        ):
            raise self._semantic_error()

    def claim_request(
        self,
        binding: MemoryActionRequestBinding,
    ) -> MemoryActionLedgerResult | None:
        self._require_active()
        if self._binding is not None:
            raise MemoryActionLedgerError("invalid_state")
        validated = _validate_binding(binding)
        digest = request_binding_digest(self._secret, validated)
        row = self._execute(
            "SELECT * FROM memory_action_requests WHERE request_id=?",
            (validated.request_id,),
        ).fetchone()
        self._binding = validated
        self._binding_digest = digest
        if row is None:
            return None
        stored_digest = row["request_binding_digest"]
        result = _safe_ledger_result(row)
        canonical_id = row["canonical_message_id"]
        snapshot = None
        if result.status == "completed":
            snapshot = self._build_terminal_semantic_snapshot(
                validated,
                canonical_message_id=canonical_id,
                result_category=result.result_category,
                result_memory_key=result.result_memory_key,
            )
        expected_digest = _terminal_binding_digest(
            self._secret,
            validated,
            canonical_message_id=canonical_id,
            status=result.status,
            result_category=result.result_category,
            result_memory_key=result.result_memory_key,
            semantic_snapshot=snapshot,
        )
        if (
            not isinstance(stored_digest, bytes)
            or len(stored_digest) != 32
            or not hmac.compare_digest(stored_digest, expected_digest)
        ):
            raise MemoryActionLedgerError("request_binding_conflict")
        if result.status == "completed":
            self._validate_terminal_semantic_snapshot(
                validated,
                snapshot,
                canonical_message_id=canonical_id,
                result_category=result.result_category,
                result_memory_key=result.result_memory_key,
                expected_action_id=None,
            )
        self._replay = result
        return result

    def _insert_canonical_action(self, *, text: str, metadata: dict) -> int:
        self._require_active()
        if (
            self._binding is None
            or self._replay is not None
            or self._canonical_message_id is not None
            or self._terminal is not None
            or not isinstance(text, str)
            or not text
            or len(text) > 4096
            or not isinstance(metadata, dict)
        ):
            raise MemoryActionLedgerError("invalid_request")
        try:
            encoded_meta = json.dumps(
                metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if len(encoded_meta.encode("utf-8")) > 16 * 1024:
                raise MemoryActionLedgerError("invalid_request")
        except (TypeError, ValueError, UnicodeError):
            raise MemoryActionLedgerError("invalid_request") from None
        cursor = self._execute(
            """INSERT INTO messages(ts,direction,kind,text,meta)
               VALUES(?,'in','user',?,?)""",
            (channel_store.now_iso(), text, encoded_meta),
        )
        self._canonical_message_id = int(cursor.lastrowid)
        return self._canonical_message_id

    def _store_connection(self, store: object) -> _CoordinatedStoreConnection:
        self._require_active()
        if (
            store is not self._store
            or self._binding is None
            or self._replay is not None
            or self._canonical_message_id is None
            or self._terminal is not None
            or self._store_completed
            or self._store_failed
        ):
            raise MemoryActionLedgerError("invalid_state")
        return _CoordinatedStoreConnection(self)

    def _validate_store_action(
        self,
        store: object,
        action: object,
    ) -> None:
        self._require_active()
        binding = self._binding
        if (
            store is not self._store
            or binding is None
            or self._replay is not None
            or self._canonical_message_id is None
            or self._terminal is not None
            or self._store_completed
            or self._store_failed
        ):
            raise MemoryActionLedgerError("invalid_state")
        if binding.action_kind == "remember":
            expected_action_type = (
                memory_runtime.ACTION_CONFIRM_DECISION
                if binding.kind == "decision"
                else memory_runtime.ACTION_REMEMBER_USER
            )
            expected_memory_key = ""
        elif binding.action_kind == "correct":
            expected_action_type = memory_runtime.ACTION_CORRECT_USER
            expected_memory_key = binding.target_memory_key
        else:
            expected_action_type = memory_runtime.ACTION_FORGET_USER
            expected_memory_key = binding.target_memory_key
        try:
            conflicts = (
                action.action_type != expected_action_type
                or action.canonical_message_id != self._canonical_message_id
                or action.kind != binding.kind
                or action.scope_type != binding.scope_type
                or action.scope_ref != binding.scope_ref
                or action.normalized_content != binding.normalized_content
                or action.sensitivity != binding.sensitivity
                or action.memory_key != expected_memory_key
            )
        except (AttributeError, TypeError):
            raise MemoryActionLedgerError("invalid_state") from None
        if conflicts:
            raise MemoryActionLedgerError("request_binding_conflict")

    def _defer_action(self, action_id: str) -> None:
        self._require_active()
        if (
            not self._store_completed
            or self._store_failed
            or not isinstance(action_id, str)
            or not action_id
            or action_id in self._deferred_actions
        ):
            raise MemoryActionLedgerError("invalid_state")
        self._deferred_actions.append(action_id)

    def complete_request(
        self,
        *,
        result_category: str,
        result_memory_key: str | None,
    ) -> MemoryActionLedgerResult:
        self._require_active()
        binding = self._binding
        if (
            binding is None
            or self._binding_digest is None
            or self._replay is not None
            or self._terminal is not None
            or self._canonical_message_id is None
            or not self._store_completed
            or self._store_failed
            or result_category not in COMPLETED_CATEGORIES[binding.action_kind]
            or (
                result_memory_key is not None
                and (
                    not isinstance(result_memory_key, str)
                    or MEMORY_KEY_PATTERN.fullmatch(result_memory_key) is None
                )
            )
        ):
            raise MemoryActionLedgerError("invalid_state")
        if len(self._deferred_actions) != 1:
            raise self._semantic_error()
        snapshot = self._build_terminal_semantic_snapshot(
            binding,
            canonical_message_id=self._canonical_message_id,
            result_category=result_category,
            result_memory_key=result_memory_key,
        )
        self._validate_terminal_semantic_snapshot(
            binding,
            snapshot,
            canonical_message_id=self._canonical_message_id,
            result_category=result_category,
            result_memory_key=result_memory_key,
            expected_action_id=self._deferred_actions[0],
        )
        stamp = channel_store.now_iso()
        terminal_digest = _terminal_binding_digest(
            self._secret,
            binding,
            canonical_message_id=self._canonical_message_id,
            status="completed",
            result_category=result_category,
            result_memory_key=result_memory_key,
            semantic_snapshot=snapshot,
        )
        self._execute(
            """INSERT INTO memory_action_requests
               (request_id,action_kind,origin,request_binding_digest,
                target_memory_key,canonical_message_id,result_memory_key,status,
                result_category,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'completed',?,?,?)""",
            (
                binding.request_id,
                binding.action_kind,
                binding.origin,
                terminal_digest,
                binding.target_memory_key,
                self._canonical_message_id,
                result_memory_key,
                result_category,
                stamp,
                stamp,
            ),
        )
        self._terminal = MemoryActionLedgerResult(
            request_id=binding.request_id,
            action_kind=binding.action_kind,
            status="completed",
            result_category=result_category,
            result_memory_key=result_memory_key,
        )
        return self._terminal

    def commit(self) -> MemoryActionLedgerResult:
        conn = self._require_active()
        result = self._replay or self._terminal
        if result is None or self._store_failed:
            raise MemoryActionLedgerError("invalid_state")
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:
            uncertain = not conn.in_transaction
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    uncertain = True
            self._finish_deferred_actions(consumed=uncertain)
            conn.close()
            self._closed = True
            self._connection = None
            raise MemoryActionLedgerError(
                "transaction_outcome_uncertain"
                if uncertain else "storage_unavailable"
            ) from None
        self._finish_deferred_actions(consumed=True)
        conn.close()
        self._closed = True
        self._connection = None
        return result

    def _finish_deferred_actions(self, *, consumed: bool) -> None:
        actions = tuple(self._deferred_actions)
        self._deferred_actions.clear()
        for action_id in actions:
            self._store._finish_action(action_id, consumed=consumed)

    def rollback(self) -> None:
        conn = self._connection
        if conn is not None:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            finally:
                conn.close()
        self._finish_deferred_actions(consumed=False)
        self._connection = None
        self._closed = True


def _new_unit_of_work(*, store: object, secret: str) -> _MemoryActionUnitOfWork:
    return _MemoryActionUnitOfWork(
        _UNIT_OF_WORK_TOKEN,
        store=store,
        secret=secret,
    )
