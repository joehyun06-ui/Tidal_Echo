"""Internal request-ledger and transaction coordination for explicit Memory actions."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
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
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
MEMORY_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
SCOPE_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ACTION_KINDS = frozenset({"remember", "correct", "forget"})
ORIGINS = frozenset({"operator_cli", "mcp", "telegram", "operit"})
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
        )
    ):
        raise MemoryActionLedgerError("invalid_request")
    if (
        status == "failed"
        and (
            canonical_message_id is not None
            or result_memory_key is not None
            or result_category not in FAILED_CATEGORIES
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
        try:
            expected_digest = _terminal_binding_digest(
                self._secret,
                validated,
                canonical_message_id=row["canonical_message_id"],
                status=result.status,
                result_category=result.result_category,
                result_memory_key=result.result_memory_key,
            )
        except MemoryActionLedgerError:
            raise MemoryActionLedgerError("memory_schema_invalid") from None
        if (
            not isinstance(stored_digest, bytes)
            or len(stored_digest) != 32
            or not hmac.compare_digest(stored_digest, expected_digest)
        ):
            raise MemoryActionLedgerError("request_binding_conflict")
        canonical_id = row["canonical_message_id"]
        if result.status == "completed":
            if self._execute(
                "SELECT 1 FROM messages WHERE id=?",
                (canonical_id,),
            ).fetchone() is None:
                raise MemoryActionLedgerError("memory_schema_invalid")
            evidence_count = int(self._execute(
                """SELECT count(*) FROM memory_evidence_events
                   WHERE canonical_message_id=?""",
                (canonical_id,),
            ).fetchone()[0])
            if (
                result.result_category == "suppressed"
                and evidence_count != 0
            ) or (
                result.result_category != "suppressed"
                and evidence_count != 1
            ):
                raise MemoryActionLedgerError("memory_schema_invalid")
            if (
                result.result_memory_key is not None
                and self._execute(
                    "SELECT 1 FROM memory_items WHERE memory_key=?",
                    (result.result_memory_key,),
                ).fetchone() is None
            ):
                raise MemoryActionLedgerError("memory_schema_invalid")
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
            or type(action) is not memory_runtime.MemoryActionBinding
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
        if (
            action.action_type != expected_action_type
            or action.canonical_message_id != self._canonical_message_id
            or action.kind != binding.kind
            or action.scope_type != binding.scope_type
            or action.scope_ref != binding.scope_ref
            or action.normalized_content != binding.normalized_content
            or action.sensitivity != binding.sensitivity
            or action.memory_key != expected_memory_key
        ):
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
                and MEMORY_KEY_PATTERN.fullmatch(result_memory_key) is None
            )
        ):
            raise MemoryActionLedgerError("invalid_state")
        stamp = channel_store.now_iso()
        terminal_digest = _terminal_binding_digest(
            self._secret,
            binding,
            canonical_message_id=self._canonical_message_id,
            status="completed",
            result_category=result_category,
            result_memory_key=result_memory_key,
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
