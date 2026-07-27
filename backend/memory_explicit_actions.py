"""Typed, origin-bound entry contracts for explicit Memory actions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

try:
    from . import (
        memory_action_ledger,
        memory_policy,
        memory_runtime,
        memory_service,
        memory_store,
    )
except ImportError:
    import memory_action_ledger
    import memory_policy
    import memory_runtime
    import memory_service
    import memory_store


_MEMORY_KEY = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
_SERVICE_CONSTRUCTOR_TOKEN = object()
_ORIGIN_PROJECTIONS = {
    "operator_cli": ("web", "relay"),
    "mcp": ("relay", "mcp"),
    "telegram": ("telegram", "telegram"),
    "operit": ("operit_share", "operit"),
}
_COMPLETED_CATEGORIES = {
    "remember": frozenset({"created", "idempotent_existing", "suppressed"}),
    "correct": frozenset({"corrected", "unchanged", "suppressed"}),
    "forget": frozenset({"forgotten", "already_forgotten"}),
}


class ExplicitMemoryActionError(RuntimeError):
    """A stable, data-free explicit-entry failure."""

    def __init__(self, category: str):
        safe = (
            category
            if isinstance(category, str)
            and category
            and category.isascii()
            and all(character.isalnum() or character == "_" for character in category)
            else "memory_operation_failed"
        )
        super().__init__(safe)
        self.category = safe


@dataclass(frozen=True, slots=True, repr=False)
class RememberExplicitMemoryRequest:
    request_id: str = field(repr=False)
    kind: str
    scope_type: str
    scope_ref: str = field(repr=False)
    content: str = field(repr=False)
    sensitivity: str


@dataclass(frozen=True, slots=True, repr=False)
class CorrectExplicitMemoryRequest:
    request_id: str = field(repr=False)
    memory_key: str = field(repr=False)
    replacement_content: str = field(repr=False)
    sensitivity: str


@dataclass(frozen=True, slots=True, repr=False)
class ForgetExplicitMemoryRequest:
    request_id: str = field(repr=False)
    memory_key: str = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class ExplicitMemoryActionResult:
    request_id: str = field(repr=False)
    action_kind: str
    status: str
    category: str
    memory_key: str | None = field(repr=False)
    kind: str
    scope_type: str
    sensitivity: str
    replayed: bool


def issue_request_id() -> str:
    return memory_action_ledger.issue_request_id()


class ExplicitMemoryActionService:
    """A fixed-origin façade; construction is reserved for the composition root."""

    __slots__ = ("_backend", "_origin", "_channel", "_source")

    def __init__(self, token: object, *, backend: object, origin: str):
        if token is not _SERVICE_CONSTRUCTOR_TOKEN or origin not in _ORIGIN_PROJECTIONS:
            raise ExplicitMemoryActionError("entry_composition_invalid")
        self._backend = backend
        self._origin = origin
        self._channel, self._source = _ORIGIN_PROJECTIONS[origin]

    def __repr__(self) -> str:
        return "<ExplicitMemoryActionService>"

    @staticmethod
    def _validate_result(result: object, *, request_id: str, action_kind: str):
        if type(result) is not ExplicitMemoryActionResult:
            raise ExplicitMemoryActionError("memory_operation_failed")
        key_required = result.category != "suppressed"
        if (
            result.request_id != request_id
            or result.action_kind != action_kind
            or result.status != "completed"
            or result.category not in _COMPLETED_CATEGORIES[action_kind]
            or type(result.replayed) is not bool
            or result.kind not in memory_policy.KINDS
            or result.scope_type not in memory_policy.SCOPE_TYPES
            or result.sensitivity not in memory_policy.SENSITIVITIES
            or (
                key_required
                and (
                    not isinstance(result.memory_key, str)
                    or _MEMORY_KEY.fullmatch(result.memory_key) is None
                )
            )
            or (not key_required and result.memory_key is not None)
        ):
            raise ExplicitMemoryActionError("memory_operation_failed")
        return result

    def _execute(self, method_name: str, request: object, action_kind: str):
        try:
            result = getattr(self._backend, method_name)(
                request,
                origin=self._origin,
                channel=self._channel,
                source=self._source,
            )
            return self._validate_result(
                result,
                request_id=request.request_id,
                action_kind=action_kind,
            )
        except ExplicitMemoryActionError:
            raise
        except (
            memory_action_ledger.MemoryActionLedgerError,
            memory_policy.MemoryPolicyError,
            memory_runtime.MemoryRuntimeError,
            memory_service.MemoryServiceError,
            memory_store.MemoryStoreError,
        ) as error:
            raise ExplicitMemoryActionError(
                str(getattr(error, "category", "memory_operation_failed"))
            ) from None
        except (AttributeError, TypeError, ValueError):
            raise ExplicitMemoryActionError("memory_operation_failed") from None

    def remember_explicit_user_memory(self, request: RememberExplicitMemoryRequest):
        if type(request) is not RememberExplicitMemoryRequest:
            raise ExplicitMemoryActionError("invalid_request")
        return self._execute("remember", request, "remember")

    def correct_explicit_user_memory(self, request: CorrectExplicitMemoryRequest):
        if type(request) is not CorrectExplicitMemoryRequest:
            raise ExplicitMemoryActionError("invalid_request")
        return self._execute("correct", request, "correct")

    def forget_explicit_user_memory(self, request: ForgetExplicitMemoryRequest):
        if type(request) is not ForgetExplicitMemoryRequest:
            raise ExplicitMemoryActionError("invalid_request")
        return self._execute("forget", request, "forget")


def _bind(backend: object, origin: str) -> ExplicitMemoryActionService:
    return ExplicitMemoryActionService(
        _SERVICE_CONSTRUCTOR_TOKEN,
        backend=backend,
        origin=origin,
    )


def bind_operator_cli(backend: object) -> ExplicitMemoryActionService:
    return _bind(backend, "operator_cli")


def bind_mcp(backend: object) -> ExplicitMemoryActionService:
    return _bind(backend, "mcp")


def bind_telegram(backend: object) -> ExplicitMemoryActionService:
    return _bind(backend, "telegram")


def bind_operit(backend: object) -> ExplicitMemoryActionService:
    return _bind(backend, "operit")
