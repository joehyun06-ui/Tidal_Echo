"""Internal-only orchestration for explicit Memory Core operations."""

from __future__ import annotations

import re
from typing import Iterable, Sequence

try:
    from . import memory_policy, memory_store
except ImportError:  # support direct module execution in local tooling
    import memory_policy
    import memory_store


_MEMORY_KEY = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")


class MemoryServiceError(RuntimeError):
    """A stable, data-free service error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class MemoryService:
    """Phase 1 service: explicit create/correct/forget and bounded internal reads."""

    HARD_MAX_RETRIEVAL_ITEMS = 20
    HARD_MAX_RETRIEVAL_CHARS = 8000

    def __init__(self, path: str, config):
        self.config = config
        self.store = memory_store.MemoryStore(path)
        self.policy = memory_policy.MemoryPolicy(
            max_item_chars=config.max_item_chars,
            sensitive_storage_enabled=config.sensitive_storage_enabled,
        )

    def readiness(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, ""
        if not self.config.configuration_valid:
            return False, self.config.error_category or "memory_configuration_invalid"
        if not self.store.validate_schema():
            return False, "memory_schema_invalid"
        return True, ""

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise MemoryServiceError("feature_disabled")
        if not self.config.configuration_valid:
            raise MemoryServiceError("memory_configuration_invalid")

    def _require_write(self) -> None:
        self._require_enabled()
        if not self.config.explicit_writes_enabled:
            raise MemoryServiceError("explicit_writes_disabled")

    @staticmethod
    def _validate_memory_key(memory_key: str) -> str:
        if not isinstance(memory_key, str) or _MEMORY_KEY.fullmatch(memory_key) is None:
            raise MemoryServiceError("invalid_memory_key")
        return memory_key

    @staticmethod
    def _translate_error(error: Exception) -> MemoryServiceError:
        category = getattr(error, "category", "memory_operation_failed")
        return MemoryServiceError(str(category))

    def _fingerprint(
        self, *, scope_type: str, scope_ref: str, kind: str, normalized_content: str,
    ) -> bytes:
        try:
            return memory_policy.fingerprint_content(
                self.config.fingerprint_hmac_secret,
                scope_type=scope_type,
                scope_ref=scope_ref,
                kind=kind,
                normalized_content=normalized_content,
            )
        except memory_policy.MemoryPolicyError as exc:
            raise self._translate_error(exc) from None

    def create_explicit_memory(
        self,
        *,
        kind: str,
        scope_type: str,
        scope_ref: str,
        content: str,
        sensitivity: str,
        sources: Iterable[memory_policy.ProvenanceInput],
    ) -> dict:
        self._require_write()
        try:
            normalized, validated_sources = self.policy.validate_explicit_create(
                kind=kind,
                scope_type=scope_type,
                scope_ref=scope_ref,
                content=content,
                sensitivity=sensitivity,
                sources=sources,
            )
            fingerprint = self._fingerprint(
                scope_type=scope_type,
                scope_ref=scope_ref,
                kind=kind,
                normalized_content=normalized,
            )
            if self.store.is_suppressed(
                scope_type=scope_type,
                scope_ref=scope_ref,
                kind=kind,
                fingerprint=fingerprint,
                fingerprint_version=memory_policy.FINGERPRINT_VERSION,
            ):
                return {"outcome": "suppressed"}
            result = self.store.create_item_with_sources(
                kind=kind,
                scope_type=scope_type,
                scope_ref=scope_ref,
                normalized_content=normalized,
                fingerprint=fingerprint,
                fingerprint_version=memory_policy.FINGERPRINT_VERSION,
                sensitivity=sensitivity,
                sources=validated_sources,
            )
            return {"outcome": result.outcome, "memory": result.item}
        except (memory_policy.MemoryPolicyError, memory_store.MemoryStoreError) as exc:
            raise self._translate_error(exc) from None

    def correct_memory(
        self,
        *,
        memory_key: str,
        content: str,
        sensitivity: str,
        sources: Iterable[memory_policy.ProvenanceInput],
    ) -> dict:
        self._require_write()
        self._validate_memory_key(memory_key)
        try:
            current = self.store.get_item_by_key(memory_key)
            if current is None:
                raise MemoryServiceError("not_found")
            if current["status"] != "active":
                raise MemoryServiceError("invalid_state")
            sensitivity_rank = {"normal": 0, "sensitive": 1, "restricted": 2}
            if (
                sensitivity not in sensitivity_rank
                or sensitivity_rank[sensitivity] < sensitivity_rank[current["sensitivity"]]
            ):
                raise MemoryServiceError("sensitivity_downgrade")
            normalized, validated_sources = self.policy.validate_explicit_create(
                kind=current["kind"],
                scope_type=current["scope_type"],
                scope_ref=current["scope_ref"],
                content=content,
                sensitivity=sensitivity,
                sources=sources,
            )
            fingerprint = self._fingerprint(
                scope_type=current["scope_type"],
                scope_ref=current["scope_ref"],
                kind=current["kind"],
                normalized_content=normalized,
            )
            result = self.store.correct_item_atomic(
                memory_key=memory_key,
                normalized_content=normalized,
                fingerprint=fingerprint,
                fingerprint_version=memory_policy.FINGERPRINT_VERSION,
                sensitivity=sensitivity,
                sources=validated_sources,
            )
            return {"outcome": result.outcome, "memory": result.item}
        except MemoryServiceError:
            raise
        except (memory_policy.MemoryPolicyError, memory_store.MemoryStoreError) as exc:
            raise self._translate_error(exc) from None

    def forget_memory(self, *, memory_key: str) -> dict:
        self._require_write()
        self._validate_memory_key(memory_key)
        try:
            result = self.store.forget_item_atomic(memory_key=memory_key)
            return {
                "outcome": result.outcome,
                "memory_key": result.item["memory_key"] if result.item is not None else memory_key,
            }
        except memory_store.MemoryStoreError as exc:
            raise self._translate_error(exc) from None

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
            self.policy.validate_scope(scope_type, scope_ref)
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
            items = self.store.get_active_items(
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
                safe["provenance"] = self.store.get_sources(item["memory_key"])
                result.append(safe)
                used += len(content)
            return result
        except memory_store.MemoryStoreError as exc:
            raise self._translate_error(exc) from None

    def get_memory_provenance(self, *, memory_key: str) -> list[dict]:
        self._require_enabled()
        self._validate_memory_key(memory_key)
        try:
            if self.store.get_item_by_key(memory_key) is None:
                raise MemoryServiceError("not_found")
            return self.store.get_sources(memory_key)
        except MemoryServiceError:
            raise
        except memory_store.MemoryStoreError as exc:
            raise self._translate_error(exc) from None

    def propose_memory_candidate(self, **_kwargs):
        raise MemoryServiceError("not_implemented_phase_1")

    def confirm_memory(self, **_kwargs):
        raise MemoryServiceError("not_implemented_phase_1")
