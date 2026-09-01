"""Kelivo-only, transient Memory context preparation with no I/O of its own."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

try:
    from . import (
        memory_context,
        memory_context_v2,
        memory_retrieval,
        memory_retrieval_v2_active,
        memory_retrieval_v2_shadow,
    )
except ImportError:  # support direct module execution in local tooling
    import memory_context
    import memory_context_v2
    import memory_retrieval
    import memory_retrieval_v2_active
    import memory_retrieval_v2_shadow


LEGACY_RETRIEVAL_MAX_ITEMS = 10
LEGACY_RETRIEVAL_CHARACTER_BUDGET = 2000
SMART_CANDIDATE_MAX_ITEMS = 20
SMART_CANDIDATE_CHARACTER_BUDGET = 8000
SMART_FINAL_MAX_ITEMS = 10
SMART_FINAL_CHARACTER_BUDGET = 2000
CLIENT_MAX_MESSAGES = 100
BASE_PROVIDER_MAX_MESSAGES = CLIENT_MAX_MESSAGES + 1
TRANSIENT_DISPATCH_MAX_MESSAGES = BASE_PROVIDER_MAX_MESSAGES + 1
MAX_CONTENT_CHARS = 32_000
_ERROR_CATEGORY = "memory_context_unavailable"
_ROLES = frozenset({"system", "developer", "user", "assistant"})


class MemoryReadServiceProtocol(Protocol):
    def get_active_memories(
        self,
        *,
        scope_type: str,
        scope_ref: str,
        kinds: Sequence[str] | None = None,
        limit: int = 20,
        character_budget: int = 8000,
        include_sensitive: bool = False,
    ) -> list[dict]: ...


class MemoryContextIntegrationError(RuntimeError):
    """Fixed, data-free failure for transient Memory preparation."""

    __slots__ = ("category",)

    def __init__(self):
        self.category = _ERROR_CATEGORY
        super().__init__(_ERROR_CATEGORY)

    def __str__(self) -> str:
        return _ERROR_CATEGORY

    def __repr__(self) -> str:
        return "MemoryContextIntegrationError('memory_context_unavailable')"


@dataclass(frozen=True, slots=True, repr=False)
class TransientMemoryDispatch:
    provider_messages: tuple[dict[str, str], ...] = field(repr=False)
    memory_applied: bool
    retrieval_v2_shadow_report: (
        memory_retrieval_v2_shadow.MemoryRetrievalV2ShadowReport | None
    ) = field(default=None, repr=False)
    retrieval_v2_active_report: (
        memory_retrieval_v2_active.MemoryRetrievalV2ActiveReport | None
    ) = field(default=None, repr=False)
    # Internal-only comparison handle for later shadow retrieval.  These keys are
    # derived from the exact provider-visible authority selection and are never
    # rendered into provider messages, repr, or telemetry.
    authoritative_memory_keys: tuple[str, ...] | None = field(
        default=None,
        repr=False,
    )

    def __repr__(self) -> str:
        return (
            "<TransientMemoryDispatch "
            f"memory_applied={self.memory_applied}>"
        )


def _valid_shadow_memory_key(value: object) -> bool:
    return (
        type(value) is str
        and value.isascii()
        and 16 <= len(value) <= 128
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _shadow_memory_keys_from_dicts(items: object) -> tuple[str, ...] | None:
    """Best-effort internal key projection; never changes context authority."""

    try:
        if type(items) not in (list, tuple) or len(items) > SMART_FINAL_MAX_ITEMS:
            return None
        keys: list[str] = []
        seen: set[str] = set()
        for item in items:
            if type(item) is not dict:
                return None
            key = item.get("memory_key")
            if not _valid_shadow_memory_key(key) or key in seen:
                return None
            seen.add(key)
            keys.append(key)
        return tuple(keys)
    except BaseException:
        return None


def _shadow_memory_keys_from_v2(selection: object) -> tuple[str, ...] | None:
    """Project V2 selected candidate keys without invoking caller hooks."""

    try:
        items = object.__getattribute__(selection, "items")
        if type(items) is not tuple or len(items) > SMART_FINAL_MAX_ITEMS:
            return None
        candidates: list[dict] = []
        for item in items:
            candidate = object.__getattribute__(item, "candidate")
            if type(candidate) is not dict:
                return None
            candidates.append(candidate)
        return _shadow_memory_keys_from_dicts(tuple(candidates))
    except BaseException:
        return None


def _validate_base_messages(base_messages: object) -> tuple[dict[str, str], ...]:
    try:
        if (
            type(base_messages) is not tuple
            or not 1 <= len(base_messages) <= BASE_PROVIDER_MAX_MESSAGES
        ):
            raise MemoryContextIntegrationError()
        for message in base_messages:
            if (
                type(message) is not dict
                or set(message) != {"role", "content"}
                or message.get("role") not in _ROLES
                or type(message.get("content")) is not str
                or len(message["content"]) > MAX_CONTENT_CHARS
            ):
                raise MemoryContextIntegrationError()
            message["content"].encode("utf-8", errors="strict")
        if (
            base_messages[-1]["role"] != "user"
            or not base_messages[-1]["content"].strip()
        ):
            raise MemoryContextIntegrationError()
        return base_messages
    except MemoryContextIntegrationError:
        raise
    except Exception:
        raise MemoryContextIntegrationError() from None


def _validated_selection_items(
    selection: object,
    *,
    candidates: tuple[dict, ...],
) -> tuple[dict, ...]:
    try:
        if type(selection) is not memory_retrieval.MemoryRetrievalSelectionV1:
            raise MemoryContextIntegrationError()
        if type(candidates) is not tuple or any(
            type(candidate) is not dict for candidate in candidates
        ):
            raise MemoryContextIntegrationError()
        items = selection.items
        candidate_count = selection.candidate_count
        selected_count = selection.selected_count
        query_signal_count = selection.query_signal_count
        if (
            type(items) is not tuple
            or type(candidate_count) is not int
            or not 0 <= candidate_count <= SMART_CANDIDATE_MAX_ITEMS
            or candidate_count != len(candidates)
            or type(selected_count) is not int
            or selected_count != len(items)
            or not 0 <= selected_count <= min(
                candidate_count, SMART_FINAL_MAX_ITEMS
            )
            or type(query_signal_count) is not int
            or not 0 <= query_signal_count <= memory_retrieval.QUERY_MAX_CHARS
            or (selected_count > 0 and query_signal_count == 0)
        ):
            raise MemoryContextIntegrationError()

        remaining_candidates = list(candidates)
        total_chars = 0
        for item in items:
            if type(item) is not dict:
                raise MemoryContextIntegrationError()
            matched_index = None
            for index, candidate in enumerate(remaining_candidates):
                if item == candidate:
                    matched_index = index
                    break
            if matched_index is None:
                raise MemoryContextIntegrationError()
            remaining_candidates.pop(matched_index)
            content = item["normalized_content"]
            if type(content) is not str:
                raise MemoryContextIntegrationError()
            total_chars += len(content)
            if total_chars > SMART_FINAL_CHARACTER_BUDGET:
                raise MemoryContextIntegrationError()
        return items
    except MemoryContextIntegrationError:
        raise
    except Exception:
        raise MemoryContextIntegrationError() from None


def prepare_transient_memory_dispatch(
    read_service: MemoryReadServiceProtocol,
    base_messages: tuple[dict[str, str], ...],
    *,
    enabled: bool,
    smart_retrieval_enabled: bool,
    retrieval_v2_shadow_enabled: bool = False,
    retrieval_v2_active_enabled: bool = False,
) -> TransientMemoryDispatch:
    """Read once, validate, and insert transient global-user Memory context."""

    if (
        type(enabled) is not bool
        or type(smart_retrieval_enabled) is not bool
        or type(retrieval_v2_shadow_enabled) is not bool
        or type(retrieval_v2_active_enabled) is not bool
    ):
        raise MemoryContextIntegrationError()
    if retrieval_v2_active_enabled and not smart_retrieval_enabled:
        raise MemoryContextIntegrationError()
    if retrieval_v2_active_enabled and retrieval_v2_shadow_enabled:
        raise MemoryContextIntegrationError()
    if retrieval_v2_shadow_enabled and not smart_retrieval_enabled:
        raise MemoryContextIntegrationError()
    if smart_retrieval_enabled and not enabled:
        raise MemoryContextIntegrationError()
    if not enabled:
        return TransientMemoryDispatch(base_messages, False)

    messages = _validate_base_messages(base_messages)
    try:
        shadow_report = None
        active_report = None
        authoritative_memory_keys = None
        if smart_retrieval_enabled:
            safe_items = read_service.get_active_memories(
                scope_type="global_user",
                scope_ref="",
                limit=SMART_CANDIDATE_MAX_ITEMS,
                character_budget=SMART_CANDIDATE_CHARACTER_BUDGET,
                include_sensitive=False,
            )
            if type(safe_items) not in (list, tuple) or any(
                type(item) is not dict for item in safe_items
            ):
                raise MemoryContextIntegrationError()
            candidate_snapshot = tuple(dict(item) for item in safe_items)
            if retrieval_v2_active_enabled:
                active_selection = (
                    memory_retrieval_v2_active.plan_memory_recall_v2_active(
                        candidate_snapshot,
                        query_text=messages[-1]["content"],
                    )
                )
                authoritative_memory_keys = _shadow_memory_keys_from_v2(
                    active_selection
                )
                developer_message = (
                    memory_context_v2.render_memory_developer_message_v2(
                        active_selection,
                        scope_type="global_user",
                    )
                )
                active_report = (
                    memory_retrieval_v2_active.active_report_from_selection(
                        active_selection
                    )
                )
            else:
                selector_input = tuple(dict(item) for item in candidate_snapshot)
                selection = memory_retrieval.select_relevant_memory_items(
                    selector_input,
                    query_text=messages[-1]["content"],
                    scope_type="global_user",
                    max_items=SMART_FINAL_MAX_ITEMS,
                    character_budget=SMART_FINAL_CHARACTER_BUDGET,
                )
                render_items = _validated_selection_items(
                    selection,
                    candidates=candidate_snapshot,
                )
                authoritative_memory_keys = _shadow_memory_keys_from_dicts(
                    render_items
                )
                if retrieval_v2_shadow_enabled:
                    try:
                        shadow_report = (
                            memory_retrieval_v2_shadow
                            .compare_memory_retrieval_v2_shadow(
                                candidate_snapshot,
                                render_items,
                                query_text=messages[-1]["content"],
                            )
                        )
                    except BaseException:
                        shadow_report = (
                            memory_retrieval_v2_shadow
                            .MemoryRetrievalV2ShadowReport.failed()
                        )
                developer_message = memory_context.render_memory_developer_message(
                    render_items,
                    scope_type="global_user",
                    max_items=SMART_FINAL_MAX_ITEMS,
                    character_budget=SMART_FINAL_CHARACTER_BUDGET,
                )
        else:
            render_items = read_service.get_active_memories(
                scope_type="global_user",
                scope_ref="",
                limit=LEGACY_RETRIEVAL_MAX_ITEMS,
                character_budget=LEGACY_RETRIEVAL_CHARACTER_BUDGET,
                include_sensitive=False,
            )
            authoritative_memory_keys = _shadow_memory_keys_from_dicts(render_items)
            developer_message = memory_context.render_memory_developer_message(
                render_items,
                scope_type="global_user",
                max_items=SMART_FINAL_MAX_ITEMS,
                character_budget=SMART_FINAL_CHARACTER_BUDGET,
            )
        if developer_message is None:
            return TransientMemoryDispatch(
                messages,
                False,
                shadow_report,
                active_report,
                authoritative_memory_keys,
            )
        if (
            type(developer_message) is not dict
            or set(developer_message) != {"role", "content"}
            or developer_message.get("role") != "developer"
            or type(developer_message.get("content")) is not str
            or not developer_message["content"]
            or len(developer_message["content"]) > MAX_CONTENT_CHARS
        ):
            raise MemoryContextIntegrationError()
        developer_message["content"].encode("utf-8", errors="strict")
        provider_messages = (
            *messages[:-1],
            dict(developer_message),
            messages[-1],
        )
        if (
            len(provider_messages) > TRANSIENT_DISPATCH_MAX_MESSAGES
            or provider_messages[-1]["role"] != "user"
        ):
            raise MemoryContextIntegrationError()
        return TransientMemoryDispatch(
            provider_messages,
            True,
            shadow_report,
            active_report,
            authoritative_memory_keys,
        )
    except MemoryContextIntegrationError:
        raise
    except Exception:
        raise MemoryContextIntegrationError() from None


__all__ = (
    "MemoryContextIntegrationError",
    "TransientMemoryDispatch",
    "prepare_transient_memory_dispatch",
)
