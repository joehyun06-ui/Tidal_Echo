"""Read-only Memory service and authority-bound privileged actions."""

from __future__ import annotations

import re
from typing import Sequence

try:
    from . import memory_policy, memory_runtime, memory_store
except ImportError:  # support direct module execution in local tooling
    import memory_policy
    import memory_runtime
    import memory_store


_MEMORY_KEY = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")


class MemoryServiceError(RuntimeError):
    """A stable, data-free service error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class MemoryReadService:
    """The only Memory service exposed to ordinary application components."""

    HARD_MAX_RETRIEVAL_ITEMS = 20
    HARD_MAX_RETRIEVAL_CHARS = 8000

    def __init__(
        self,
        reader: memory_store.MemoryReader,
        *,
        enabled: bool,
        configuration_valid: bool,
        error_category: str,
        explicit_writes_enabled: bool,
        policy: memory_policy.MemoryPolicy,
    ):
        self._reader = reader
        self._enabled = bool(enabled)
        self._configuration_valid = bool(configuration_valid)
        self._error_category = str(error_category)
        self._explicit_writes_enabled = bool(explicit_writes_enabled)
        self._policy = policy

    @staticmethod
    def _validate_memory_key(memory_key: str) -> str:
        if not isinstance(memory_key, str) or _MEMORY_KEY.fullmatch(memory_key) is None:
            raise MemoryServiceError("invalid_memory_key")
        return memory_key

    @staticmethod
    def _translate_error(error: Exception) -> MemoryServiceError:
        return MemoryServiceError(str(
            getattr(error, "category", "memory_operation_failed")
        ))

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise MemoryServiceError("feature_disabled")
        if not self._configuration_valid:
            raise MemoryServiceError("memory_configuration_invalid")

    def readiness(self) -> tuple[bool, str]:
        if not self._enabled:
            return False, ""
        if not self._configuration_valid:
            return False, self._error_category or "memory_configuration_invalid"
        if not self._reader.validate_schema():
            return False, "memory_schema_invalid"
        if self._explicit_writes_enabled:
            try:
                if not self._reader.validate_runtime_profile_state():
                    return False, "memory_fingerprint_profile_mismatch"
            except (memory_policy.MemoryPolicyError, memory_store.MemoryStoreError) as exc:
                return False, getattr(
                    exc, "category", "memory_fingerprint_profile_mismatch"
                )
        return True, ""

    def get_active_memories(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        kinds: Sequence[str] | None = None,
        limit: int = HARD_MAX_RETRIEVAL_ITEMS,
        character_budget: int = HARD_MAX_RETRIEVAL_CHARS,
        include_sensitive: bool = False,
    ) -> list[dict]:
        self._require_enabled()
        try:
            self._policy.validate_scope(scope_type, scope_ref)
        except memory_policy.MemoryPolicyError as exc:
            raise self._translate_error(exc) from None
        if include_sensitive:
            raise MemoryServiceError("sensitive_retrieval_disabled")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or not isinstance(character_budget, int)
            or isinstance(character_budget, bool)
            or character_budget < 1
        ):
            raise MemoryServiceError("invalid_query")
        limit = min(limit, self.HARD_MAX_RETRIEVAL_ITEMS)
        character_budget = min(character_budget, self.HARD_MAX_RETRIEVAL_CHARS)
        try:
            items = self._reader.get_active_items(
                scope_type=scope_type,
                scope_ref=scope_ref,
                kinds=kinds,
                sensitivities=("normal",),
                limit=limit,
            )
            result: list[dict] = []
            used = 0
            for item in items:
                content = item["normalized_content"]
                if not isinstance(content, str):
                    continue
                if used + len(content) > character_budget:
                    break
                safe = dict(item)
                safe["provenance"] = self._reader.get_sources(item["memory_key"])
                result.append(safe)
                used += len(content)
            return result
        except memory_store.MemoryStoreError as exc:
            raise self._translate_error(exc) from None

    def get_memory_provenance(self, *, memory_key: str) -> list[dict]:
        self._require_enabled()
        self._validate_memory_key(memory_key)
        try:
            if self._reader.get_item_by_key(memory_key) is None:
                raise MemoryServiceError("not_found")
            return self._reader.get_sources(memory_key)
        except MemoryServiceError:
            raise
        except memory_store.MemoryStoreError as exc:
            raise self._translate_error(exc) from None

    def propose_memory_candidate(self, **_kwargs):
        raise MemoryServiceError("not_implemented_phase_1")

    def confirm_memory(self, **_kwargs):
        raise MemoryServiceError("not_implemented_phase_1")


class _PrivilegedServiceBase:
    def __init__(self, store: memory_store.MemoryStore, authority: object):
        try:
            policy = memory_runtime.require_runtime_authority(authority)
        except memory_runtime.MemoryRuntimeError as error:
            raise MemoryServiceError(error.category) from None
        self._store = store
        self._authority = authority
        self._policy = store.policy

    @staticmethod
    def _validate_memory_key(memory_key: str) -> str:
        if not isinstance(memory_key, str) or _MEMORY_KEY.fullmatch(memory_key) is None:
            raise MemoryServiceError("invalid_memory_key")
        return memory_key

    @staticmethod
    def _translate_error(error: Exception) -> MemoryServiceError:
        return MemoryServiceError(str(
            getattr(error, "category", "memory_operation_failed")
        ))

    def _require_enabled(self) -> None:
        try:
            policy = memory_runtime.require_runtime_authority(self._authority)
        except memory_runtime.MemoryRuntimeError as error:
            raise self._translate_error(error) from None
        if not policy.enabled:
            raise MemoryServiceError("feature_disabled")
        if not policy.configuration_valid:
            raise MemoryServiceError("memory_configuration_invalid")


class PrivilegedMemoryActions(_PrivilegedServiceBase):
    """Narrow fixed-semantics actions retained only by the composition root."""

    def _binding(
        self,
        *,
        action_type: str,
        canonical_message_id: int,
        kind: str,
        scope_type: str,
        scope_ref: str,
        normalized_content: str | None,
        sensitivity: str,
        memory_key: str = "",
    ) -> memory_runtime.MemoryActionBinding:
        return memory_runtime.MemoryActionBinding(
            action_type=action_type,
            canonical_message_id=canonical_message_id,
            kind=kind,
            scope_type=scope_type,
            scope_ref=scope_ref,
            normalized_content=normalized_content,
            sensitivity=sensitivity,
            memory_key=memory_key,
        )

    @staticmethod
    def _one_source(
        canonical_message_id: int,
    ) -> tuple[memory_policy.ProvenanceInput, ...]:
        return (memory_policy.ProvenanceInput(
            canonical_message_id=canonical_message_id,
        ),)

    def _execute_create(
        self,
        *,
        action_type: str,
        method,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        canonical_message_id: int,
        _transaction: object | None = None,
    ) -> dict:
        self._require_enabled()
        sources = self._one_source(canonical_message_id)
        try:
            normalized, validated = self._policy.validate_explicit_create(
                kind=kind,
                scope_type=scope_type,
                scope_ref=scope_ref,
                content=content,
                sensitivity=sensitivity,
                sources=sources,
                allow_existing_reclassification=True,
            )
            envelope = memory_runtime.issue_action_envelope(
                self._authority,
                self._binding(
                    action_type=action_type,
                    canonical_message_id=canonical_message_id,
                    kind=kind,
                    scope_type=scope_type,
                    scope_ref=scope_ref,
                    normalized_content=normalized,
                    sensitivity=sensitivity,
                ),
            )
            result = method(
                kind=kind,
                scope_type=scope_type,
                scope_ref=scope_ref,
                content=content,
                sensitivity=sensitivity,
                sources=validated,
                authorization=envelope,
                _transaction=_transaction,
            )
            return {"outcome": result.outcome, "memory": result.item}
        except (
            memory_policy.MemoryPolicyError,
            memory_runtime.MemoryRuntimeError,
            memory_store.MemoryStoreError,
        ) as exc:
            raise self._translate_error(exc) from None

    def remember_explicit_user_message(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        canonical_message_id: int,
        _transaction: object | None = None,
    ) -> dict:
        if kind in {"decision", "assistant_experience"}:
            raise MemoryServiceError("unsupported_evidence")
        return self._execute_create(
            action_type=memory_runtime.ACTION_REMEMBER_USER,
            method=self._store.create_explicit_memory_from_user_action,
            kind=kind,
            scope_type=scope_type,
            scope_ref=scope_ref,
            content=content,
            sensitivity=sensitivity,
            canonical_message_id=canonical_message_id,
            _transaction=_transaction,
        )

    def confirm_project_decision(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        canonical_message_id: int,
        _transaction: object | None = None,
    ) -> dict:
        return self._execute_create(
            action_type=memory_runtime.ACTION_CONFIRM_DECISION,
            method=self._store.create_confirmed_project_decision_from_action,
            kind="decision",
            scope_type=scope_type,
            scope_ref=scope_ref,
            content=content,
            sensitivity=sensitivity,
            canonical_message_id=canonical_message_id,
            _transaction=_transaction,
        )

    def record_assistant_experience(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        canonical_message_id: int,
    ) -> dict:
        return self._execute_create(
            action_type=memory_runtime.ACTION_ASSISTANT_EXPERIENCE,
            method=self._store.create_assistant_experience_from_action,
            kind="assistant_experience",
            scope_type=scope_type,
            scope_ref=scope_ref,
            content=content,
            sensitivity=sensitivity,
            canonical_message_id=canonical_message_id,
        )

    def correct_explicit_user_memory(
        self,
        *,
        memory_key: str,
        content: str,
        sensitivity: str,
        canonical_message_id: int,
        _transaction: object | None = None,
    ) -> dict:
        self._require_enabled()
        self._validate_memory_key(memory_key)
        sources = self._one_source(canonical_message_id)
        try:
            current = self._store.get_item_by_key(memory_key)
            if current is None:
                raise MemoryServiceError("not_found")
            if current["status"] != "active":
                raise MemoryServiceError("invalid_state")
            if current["kind"] == "assistant_experience":
                raise MemoryServiceError("unsupported_evidence")
            normalized = self._policy.validate_content(
                content,
                sensitivity,
                allow_existing_reclassification=True,
            )
            validated = self._policy.validate_provenance_inputs(
                current["kind"], sources,
            )
            envelope = memory_runtime.issue_action_envelope(
                self._authority,
                self._binding(
                    action_type=memory_runtime.ACTION_CORRECT_USER,
                    canonical_message_id=canonical_message_id,
                    kind=current["kind"],
                    scope_type=current["scope_type"],
                    scope_ref=current["scope_ref"],
                    normalized_content=normalized,
                    sensitivity=sensitivity,
                    memory_key=memory_key,
                ),
            )
            result = self._store.correct_memory_from_user_action(
                memory_key=memory_key,
                content=content,
                sensitivity=sensitivity,
                sources=validated,
                authorization=envelope,
                _transaction=_transaction,
            )
            return {"outcome": result.outcome, "memory": result.item}
        except MemoryServiceError:
            raise
        except (
            memory_policy.MemoryPolicyError,
            memory_runtime.MemoryRuntimeError,
            memory_store.MemoryStoreError,
        ) as exc:
            raise self._translate_error(exc) from None

    def forget_explicit_user_memory(
        self,
        *,
        memory_key: str,
        canonical_message_id: int,
        _transaction: object | None = None,
    ) -> dict:
        self._require_enabled()
        self._validate_memory_key(memory_key)
        sources = self._one_source(canonical_message_id)
        try:
            current = self._store._get_forget_target_metadata(
                memory_key,
                _transaction=_transaction,
            )
            if current is None:
                raise MemoryServiceError("not_found")
            if type(current) is not memory_store._ForgetTargetMetadataV1:
                raise MemoryServiceError("invalid_state")
            if _transaction is not None:
                self._store._validate_forget_target_binding(
                    current,
                    _transaction=_transaction,
                )
            if current.kind == "assistant_experience":
                raise MemoryServiceError("unsupported_evidence")
            validated = self._policy.validate_provenance_inputs(
                current.kind, sources,
            )
            envelope = memory_runtime.issue_action_envelope(
                self._authority,
                self._binding(
                    action_type=memory_runtime.ACTION_FORGET_USER,
                    canonical_message_id=canonical_message_id,
                    kind=current.kind,
                    scope_type=current.scope_type,
                    scope_ref=current.scope_ref,
                    normalized_content=None,
                    sensitivity=current.sensitivity,
                    memory_key=memory_key,
                ),
            )
            result = self._store.forget_memory_atomic(
                memory_key=memory_key,
                sources=validated,
                authorization=envelope,
                _transaction=_transaction,
            )
            return {
                "outcome": result.outcome,
                "memory_key": (
                    result.item["memory_key"]
                    if result.item is not None else memory_key
                ),
            }
        except MemoryServiceError:
            raise
        except (
            memory_policy.MemoryPolicyError,
            memory_runtime.MemoryRuntimeError,
            memory_store.MemoryStoreError,
        ) as exc:
            raise self._translate_error(exc) from None


# Compatibility name for importers; it deliberately has no write methods.
MemoryService = MemoryReadService
