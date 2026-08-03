"""Kelivo-only, transient Memory context preparation with no I/O of its own."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

try:
    from . import memory_context
except ImportError:  # support direct module execution in local tooling
    import memory_context


RETRIEVAL_MAX_ITEMS = 10
RETRIEVAL_CHARACTER_BUDGET = 2000
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


def prepare_transient_memory_dispatch(
    read_service: MemoryReadServiceProtocol,
    base_messages: tuple[dict[str, str], ...],
    *,
    enabled: bool,
) -> TransientMemoryDispatch:
    """Read once, validate, and insert transient global-user Memory context."""

    if type(enabled) is not bool:
        raise MemoryContextIntegrationError()
    if not enabled:
        return TransientMemoryDispatch(base_messages, False)

    messages = _validate_base_messages(base_messages)
    try:
        safe_items = read_service.get_active_memories(
            scope_type="global_user",
            scope_ref="",
            limit=RETRIEVAL_MAX_ITEMS,
            character_budget=RETRIEVAL_CHARACTER_BUDGET,
            include_sensitive=False,
        )
        developer_message = memory_context.render_memory_developer_message(
            safe_items,
            scope_type="global_user",
            max_items=RETRIEVAL_MAX_ITEMS,
            character_budget=RETRIEVAL_CHARACTER_BUDGET,
        )
        if developer_message is None:
            return TransientMemoryDispatch(messages, False)
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
        return TransientMemoryDispatch(provider_messages, True)
    except MemoryContextIntegrationError:
        raise
    except Exception:
        raise MemoryContextIntegrationError() from None


__all__ = (
    "MemoryContextIntegrationError",
    "TransientMemoryDispatch",
    "prepare_transient_memory_dispatch",
)
