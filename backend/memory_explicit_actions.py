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

    def __repr__(self) -> str:
        return "<RememberExplicitMemoryRequest>"


@dataclass(frozen=True, slots=True, repr=False)
class CorrectExplicitMemoryRequest:
    request_id: str = field(repr=False)
    memory_key: str = field(repr=False)
    replacement_content: str = field(repr=False)
    sensitivity: str

    def __repr__(self) -> str:
        return "<CorrectExplicitMemoryRequest>"


@dataclass(frozen=True, slots=True, repr=False)
class ForgetExplicitMemoryRequest:
    request_id: str = field(repr=False)
    memory_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "<ForgetExplicitMemoryRequest>"


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

    def __repr__(self) -> str:
        return "<ExplicitMemoryActionResult>"


def issue_request_id() -> str:
    return memory_action_ledger.issue_request_id()


_ENTRY_BACKEND_CONSTRUCTOR_TOKEN = object()
_TARGET_STATUSES = frozenset(
    {"candidate", "active", "superseded", "forgotten", "rejected"}
)


class MemoryActionEntryBackend:
    """Reviewed internal owner of the PR A private transaction composition."""

    __slots__ = ("_actions", "_store", "_policy")

    def __init__(self, token: object, privileged_actions: object):
        if (
            token is not _ENTRY_BACKEND_CONSTRUCTOR_TOKEN
            or type(privileged_actions) is not memory_service.PrivilegedMemoryActions
        ):
            raise ExplicitMemoryActionError("entry_composition_invalid")
        self._actions = privileged_actions
        self._store = privileged_actions._store
        self._policy = privileged_actions._policy

    def __repr__(self) -> str:
        return "<MemoryActionEntryBackend>"

    @staticmethod
    def _validate_projection(*, origin: str, channel: str, source: str) -> None:
        if _ORIGIN_PROJECTIONS.get(origin) != (channel, source):
            raise ExplicitMemoryActionError("entry_composition_invalid")

    @staticmethod
    def _validate_memory_key(memory_key: object) -> str:
        if (
            not isinstance(memory_key, str)
            or _MEMORY_KEY.fullmatch(memory_key) is None
        ):
            raise ExplicitMemoryActionError("invalid_memory_key")
        return memory_key

    def _target(self, uow, memory_key: str) -> dict:
        row = uow._execute(
            """SELECT memory_key,kind,scope_type,scope_ref,status,sensitivity
               FROM memory_items WHERE memory_key=?""",
            (memory_key,),
        ).fetchone()
        if row is None:
            raise ExplicitMemoryActionError("not_found")
        row_memory_key = row["memory_key"]
        kind = row["kind"]
        scope_type = row["scope_type"]
        scope_ref = row["scope_ref"]
        status = row["status"]
        sensitivity = row["sensitivity"]
        if (
            type(row_memory_key) is not str
            or row_memory_key != memory_key
            or _MEMORY_KEY.fullmatch(row_memory_key) is None
            or type(status) is not str
            or status not in _TARGET_STATUSES
            or type(sensitivity) is not str
            or sensitivity not in memory_policy.SENSITIVITIES
        ):
            raise ExplicitMemoryActionError("invalid_state")
        try:
            self._policy.validate_kind(kind)
            self._policy.validate_scope(scope_type, scope_ref)
        except memory_policy.MemoryPolicyError:
            raise ExplicitMemoryActionError("invalid_state") from None
        return {
            "memory_key": row_memory_key,
            "kind": kind,
            "scope_type": scope_type,
            "scope_ref": scope_ref,
            "status": status,
            "sensitivity": sensitivity,
        }

    @staticmethod
    def _safe_result(
        terminal: memory_action_ledger.MemoryActionLedgerResult,
        binding: memory_action_ledger.MemoryActionRequestBinding,
        *,
        replayed: bool,
    ) -> ExplicitMemoryActionResult:
        return ExplicitMemoryActionResult(
            request_id=terminal.request_id,
            action_kind=terminal.action_kind,
            status=terminal.status,
            category=terminal.result_category,
            memory_key=terminal.result_memory_key,
            kind=binding.kind,
            scope_type=binding.scope_type,
            sensitivity=binding.sensitivity,
            replayed=replayed,
        )

    def _resolve_uncertain(
        self,
        binding: memory_action_ledger.MemoryActionRequestBinding,
    ) -> ExplicitMemoryActionResult:
        with self._store._action_unit_of_work() as lookup:
            replay = lookup._lookup_existing_terminal(binding)
            if replay is None:
                raise ExplicitMemoryActionError("transaction_outcome_uncertain")
            committed = lookup.commit()
        return self._safe_result(committed, binding, replayed=True)

    def _run(
        self,
        *,
        prepare_binding,
        canonical_text,
        channel: str,
        source: str,
        action,
        lookup_replay=None,
        validate_new_request=None,
    ) -> ExplicitMemoryActionResult:
        binding = None
        try:
            with self._store._action_unit_of_work() as uow:
                if lookup_replay is not None:
                    terminal = lookup_replay(uow)
                    if terminal is not None:
                        if (
                            type(terminal)
                            is not memory_action_ledger._ForgetTerminalReplayV1
                        ):
                            raise ExplicitMemoryActionError("invalid_state")
                        committed = uow.commit()
                        return self._safe_result(
                            committed,
                            terminal.binding,
                            replayed=True,
                        )
                binding = prepare_binding(uow)
                replay = uow.claim_request(binding)
                if replay is not None:
                    committed = uow.commit()
                    return self._safe_result(committed, binding, replayed=True)
                if validate_new_request is not None:
                    validate_new_request(uow, binding)
                canonical_id = uow._insert_canonical_action(
                    text=canonical_text(binding),
                    metadata={"channel": channel, "source": source},
                )
                action(uow, canonical_id, binding)
                uow.complete_request()
                committed = uow.commit()
                return self._safe_result(committed, binding, replayed=False)
        except memory_action_ledger.MemoryActionLedgerError as error:
            if (
                error.category == "transaction_outcome_uncertain"
                and binding is not None
            ):
                return self._resolve_uncertain(binding)
            raise

    def remember(self, request, *, origin: str, channel: str, source: str):
        self._validate_projection(origin=origin, channel=channel, source=source)
        if type(request) is not RememberExplicitMemoryRequest:
            raise ExplicitMemoryActionError("invalid_request")
        if request.kind == "assistant_experience":
            raise ExplicitMemoryActionError("unsupported_evidence")
        self._policy.validate_kind(request.kind)
        self._policy.validate_scope(request.scope_type, request.scope_ref)
        normalized = self._policy.validate_content(
            request.content,
            request.sensitivity,
        )
        binding = memory_action_ledger.MemoryActionRequestBinding(
            request_id=request.request_id,
            action_kind="remember",
            origin=origin,
            scope_type=request.scope_type,
            scope_ref=request.scope_ref,
            kind=request.kind,
            sensitivity=request.sensitivity,
            normalized_content=normalized,
        )

        def execute(uow, canonical_id, _binding):
            arguments = {
                "scope_type": request.scope_type,
                "scope_ref": request.scope_ref,
                "content": normalized,
                "sensitivity": request.sensitivity,
                "canonical_message_id": canonical_id,
                "_transaction": uow,
            }
            if request.kind == "decision":
                self._actions.confirm_project_decision(**arguments)
            else:
                self._actions.remember_explicit_user_message(
                    kind=request.kind,
                    **arguments,
                )

        return self._run(
            prepare_binding=lambda _uow: binding,
            canonical_text=lambda _binding: normalized,
            channel=channel,
            source=source,
            action=execute,
        )

    def correct(self, request, *, origin: str, channel: str, source: str):
        self._validate_projection(origin=origin, channel=channel, source=source)
        if type(request) is not CorrectExplicitMemoryRequest:
            raise ExplicitMemoryActionError("invalid_request")
        memory_key = self._validate_memory_key(request.memory_key)
        normalized = self._policy.validate_content(
            request.replacement_content,
            request.sensitivity,
        )
        prepared_target_status = None

        def prepare(uow):
            nonlocal prepared_target_status
            target = self._target(uow, memory_key)
            if target["kind"] == "assistant_experience":
                raise ExplicitMemoryActionError("unsupported_evidence")
            prepared_target_status = target["status"]
            return memory_action_ledger.MemoryActionRequestBinding(
                request_id=request.request_id,
                action_kind="correct",
                origin=origin,
                target_memory_key=memory_key,
                scope_type=target["scope_type"],
                scope_ref=target["scope_ref"],
                kind=target["kind"],
                sensitivity=request.sensitivity,
                normalized_content=normalized,
            )

        def validate_new_request(_uow, _binding):
            if prepared_target_status != "active":
                raise ExplicitMemoryActionError("invalid_state")

        def execute(uow, canonical_id, _binding):
            self._actions.correct_explicit_user_memory(
                memory_key=memory_key,
                content=normalized,
                sensitivity=request.sensitivity,
                canonical_message_id=canonical_id,
                _transaction=uow,
            )

        return self._run(
            prepare_binding=prepare,
            canonical_text=lambda _binding: normalized,
            channel=channel,
            source=source,
            action=execute,
            validate_new_request=validate_new_request,
        )

    def forget(self, request, *, origin: str, channel: str, source: str):
        self._validate_projection(origin=origin, channel=channel, source=source)
        if type(request) is not ForgetExplicitMemoryRequest:
            raise ExplicitMemoryActionError("invalid_request")
        memory_key = self._validate_memory_key(request.memory_key)

        def prepare(uow):
            target = self._store._get_forget_target_metadata(
                memory_key,
                _transaction=uow,
            )
            if target is None:
                raise ExplicitMemoryActionError("not_found")
            if type(target) is not memory_store._ForgetTargetMetadataV1:
                raise ExplicitMemoryActionError("invalid_state")
            uow._require_prepared_forget_target(
                store=self._store,
                metadata=target,
            )
            if target.kind == "assistant_experience":
                raise ExplicitMemoryActionError("unsupported_evidence")
            return memory_action_ledger.MemoryActionRequestBinding(
                request_id=request.request_id,
                action_kind="forget",
                origin=origin,
                target_memory_key=memory_key,
                scope_type=target.scope_type,
                scope_ref=target.scope_ref,
                kind=target.kind,
                sensitivity=target.sensitivity,
                normalized_content=None,
            )

        def execute(uow, canonical_id, _binding):
            self._actions.forget_explicit_user_memory(
                memory_key=memory_key,
                canonical_message_id=canonical_id,
                _transaction=uow,
            )

        return self._run(
            prepare_binding=prepare,
            canonical_text=lambda _binding: (
                f"Forget explicit memory: {memory_key}"
            ),
            channel=channel,
            source=source,
            action=execute,
            lookup_replay=lambda uow: uow.lookup_forget_terminal(
                request_id=request.request_id,
                origin=origin,
                target_memory_key=memory_key,
            ),
        )


def create_entry_backend(privileged_actions: object) -> MemoryActionEntryBackend:
    return MemoryActionEntryBackend(
        _ENTRY_BACKEND_CONSTRUCTOR_TOKEN,
        privileged_actions,
    )


class ExplicitMemoryActionService:
    """A fixed-origin facade; construction is reserved for the composition root."""

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
