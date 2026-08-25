"""Kelivo-only, transient Memory context preparation with no I/O of its own."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

try:
    from . import memory_context, memory_retrieval, memory_retrieval_v2_shadow
except ImportError:  # support direct module execution in local tooling
    import memory_context
    import memory_retrieval
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

    def __repr__(self) -> str:
        return (
            "<TransientMemoryDispatch "
            f"memory_applied={self.memory_applied}>"
        )


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
) -> TransientMemoryDispatch:
    """Read once, validate, and insert transient global-user Memory context."""

    if (
        type(enabled) is not bool
        or type(smart_retrieval_enabled) is not bool
        or type(retrieval_v2_shadow_enabled) is not bool
    ):
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
        else:
            render_items = read_service.get_active_memories(
                scope_type="global_user",
                scope_ref="",
                limit=LEGACY_RETRIEVAL_MAX_ITEMS,
                character_budget=LEGACY_RETRIEVAL_CHARACTER_BUDGET,
                include_sensitive=False,
            )
        developer_message = memory_context.render_memory_developer_message(
            render_items,
            scope_type="global_user",
            max_items=SMART_FINAL_MAX_ITEMS,
            character_budget=SMART_FINAL_CHARACTER_BUDGET,
        )
        if developer_message is None:
            return TransientMemoryDispatch(messages, False, shadow_report)
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
        return TransientMemoryDispatch(provider_messages, True, shadow_report)
    except MemoryContextIntegrationError:
        raise
    except Exception:
        raise MemoryContextIntegrationError() from None


__all__ = (
    "MemoryContextIntegrationError",
    "TransientMemoryDispatch",
    "prepare_transient_memory_dispatch",
)
