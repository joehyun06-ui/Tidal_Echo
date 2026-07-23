"""Process-local bootstrap authority and short-lived Memory action capabilities."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from . import deployment_config, memory_policy
except ImportError:  # support direct module execution in local tooling
    import deployment_config
    import memory_policy


ACTION_BINDING_VERSION = 1
ACTION_CAPABILITY_TTL_SECONDS = 30
ACTION_CAPABILITY_TTL_NS = ACTION_CAPABILITY_TTL_SECONDS * 1_000_000_000
_MAX_MONOTONIC_NS = (1 << 63) - 1
_ACTION_SIGNATURE_BYTES = hashlib.sha256().digest_size

ACTION_REMEMBER_USER = "remember_explicit_user"
ACTION_CONFIRM_DECISION = "confirm_project_decision"
ACTION_CORRECT_USER = "correct_explicit_user"
ACTION_FORGET_USER = "forget_explicit_user"
ACTION_ASSISTANT_EXPERIENCE = "record_assistant_experience"

ACTION_TYPES = frozenset({
    ACTION_REMEMBER_USER,
    ACTION_CONFIRM_DECISION,
    ACTION_CORRECT_USER,
    ACTION_FORGET_USER,
    ACTION_ASSISTANT_EXPERIENCE,
})


class MemoryRuntimeError(RuntimeError):
    """A stable, data-free runtime authority error."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, repr=False)
class MemoryRuntimePolicy:
    enabled: bool
    explicit_writes_enabled: bool
    sensitive_storage_enabled: bool
    max_item_chars: int
    forget_retention_policy: str
    fingerprint_key_id: str = field(repr=False)
    fingerprint_hmac_secret: str = field(repr=False)
    normalization_version: int = field(repr=False)
    fingerprint_version: int = field(repr=False)
    fingerprint_domain: str = field(repr=False)
    configuration_valid: bool
    error_category: str


@dataclass(frozen=True, repr=False, slots=True)
class MemoryActionBinding:
    action_type: str
    canonical_message_id: int
    kind: str
    scope_type: str
    scope_ref: str = field(repr=False)
    normalized_content: str | None = field(repr=False)
    sensitivity: str
    memory_key: str = field(default="", repr=False)


@dataclass(frozen=True, repr=False, slots=True)
class _MemoryActionEnvelope:
    action_id: str = field(repr=False)
    binding: MemoryActionBinding = field(repr=False)
    issued_at_ns: int = field(repr=False)
    expires_at_ns: int = field(repr=False)
    signature: bytes = field(repr=False)


class _RuntimeAuthority:
    __slots__ = (
        "_identity",
        "_policy",
        "_action_secret",
        "_action_lock",
        "_inflight_actions",
        "_consumed_actions",
    )

    def __init__(
        self,
        constructor_token: object,
        *,
        identity: object,
        policy: MemoryRuntimePolicy,
        action_secret: bytes,
    ):
        if constructor_token is not _AUTHORITY_CONSTRUCTOR_TOKEN:
            raise MemoryRuntimeError("runtime_authority_invalid")
        self._identity = identity
        self._policy = policy
        self._action_secret = action_secret
        self._action_lock = threading.Lock()
        self._inflight_actions: set[str] = set()
        self._consumed_actions: set[str] = set()

    def __repr__(self) -> str:
        return "<MemoryRuntimeAuthority>"


@dataclass(frozen=True, repr=False)
class MemoryRuntime:
    read_service: object = field(repr=False)
    privileged_actions: object = field(repr=False)


_AUTHORITY_CONSTRUCTOR_TOKEN = object()
_PROCESS_RUNTIME_IDENTITY = object()
_PROCESS_AUTHORITY: _RuntimeAuthority | None = None
_PROCESS_BOOTSTRAPPED = False
_BOOTSTRAP_LOCK = threading.Lock()


def _policy_from_config(config: deployment_config.MemoryConfig) -> MemoryRuntimePolicy:
    return MemoryRuntimePolicy(
        enabled=config.enabled,
        explicit_writes_enabled=config.explicit_writes_enabled,
        sensitive_storage_enabled=config.sensitive_storage_enabled,
        max_item_chars=config.max_item_chars,
        forget_retention_policy=config.forget_retention_policy,
        fingerprint_key_id=config.fingerprint_key_id,
        fingerprint_hmac_secret=config.fingerprint_hmac_secret,
        normalization_version=memory_policy.NORMALIZATION_VERSION,
        fingerprint_version=memory_policy.FINGERPRINT_VERSION,
        fingerprint_domain=memory_policy.FINGERPRINT_DOMAIN,
        configuration_valid=config.configuration_valid,
        error_category=config.error_category,
    )


def require_runtime_authority(authority: object) -> MemoryRuntimePolicy:
    current = _PROCESS_AUTHORITY
    if (
        type(authority) is not _RuntimeAuthority
        or authority is not current
        or current is None
        or current._identity is not _PROCESS_RUNTIME_IDENTITY
    ):
        raise MemoryRuntimeError("runtime_authority_invalid")
    return current._policy


def _binding_payload(
    *,
    action_id: str,
    binding: MemoryActionBinding,
    issued_at_ns: int,
    expires_at_ns: int,
) -> bytes:
    payload = {
        "action_id": action_id,
        "action_type": binding.action_type,
        "binding_version": ACTION_BINDING_VERSION,
        "canonical_message_id": binding.canonical_message_id,
        "expires_at_ns": expires_at_ns,
        "fingerprint_domain": memory_policy.FINGERPRINT_DOMAIN,
        "fingerprint_version": memory_policy.FINGERPRINT_VERSION,
        "issued_at_ns": issued_at_ns,
        "kind": binding.kind,
        "memory_key": binding.memory_key,
        "normalization_version": memory_policy.NORMALIZATION_VERSION,
        "normalized_content": binding.normalized_content,
        "scope_ref": binding.scope_ref,
        "scope_type": binding.scope_type,
        "sensitivity": binding.sensitivity,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _valid_monotonic_ns(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_MONOTONIC_NS


def _valid_action_id(value: object) -> bool:
    return (
        type(value) is str
        and 24 <= len(value) <= 96
        and all(
            character.isascii()
            and (character.isalnum() or character in "-_")
            for character in value
        )
    )


def _valid_binding_shape(value: object) -> bool:
    if type(value) is not MemoryActionBinding:
        return False
    try:
        return (
            type(value.action_type) is str
            and value.action_type in ACTION_TYPES
            and type(value.canonical_message_id) is int
            and value.canonical_message_id > 0
            and type(value.kind) is str
            and type(value.scope_type) is str
            and type(value.scope_ref) is str
            and (
                value.normalized_content is None
                or type(value.normalized_content) is str
            )
            and type(value.sensitivity) is str
            and type(value.memory_key) is str
        )
    except (AttributeError, TypeError):
        return False


def issue_action_envelope(
    authority: object,
    binding: MemoryActionBinding,
) -> object:
    require_runtime_authority(authority)
    if not _valid_binding_shape(binding):
        raise MemoryRuntimeError("authorization_invalid")
    action_id = secrets.token_urlsafe(24)
    issued_at_ns = time.monotonic_ns()
    if not _valid_monotonic_ns(issued_at_ns):
        raise MemoryRuntimeError("authorization_invalid")
    expires_at_ns = issued_at_ns + ACTION_CAPABILITY_TTL_NS
    if not _valid_monotonic_ns(expires_at_ns):
        raise MemoryRuntimeError("authorization_invalid")
    try:
        payload = _binding_payload(
            action_id=action_id,
            binding=binding,
            issued_at_ns=issued_at_ns,
            expires_at_ns=expires_at_ns,
        )
    except (TypeError, ValueError, UnicodeError):
        raise MemoryRuntimeError("authorization_invalid") from None
    signature = hmac.new(
        authority._action_secret,
        payload,
        hashlib.sha256,
    ).digest()
    return _MemoryActionEnvelope(
        action_id=action_id,
        binding=binding,
        issued_at_ns=issued_at_ns,
        expires_at_ns=expires_at_ns,
        signature=signature,
    )


def begin_action_consumption(
    authority: object,
    envelope: object | None,
    *,
    expected_binding: MemoryActionBinding,
) -> str:
    require_runtime_authority(authority)
    if envelope is None:
        raise MemoryRuntimeError("authorization_required")
    if type(envelope) is not _MemoryActionEnvelope:
        raise MemoryRuntimeError("authorization_invalid")
    if not _valid_binding_shape(expected_binding):
        raise MemoryRuntimeError("authorization_invalid")
    try:
        action_id = envelope.action_id
        binding = envelope.binding
        issued_at_ns = envelope.issued_at_ns
        expires_at_ns = envelope.expires_at_ns
        signature = envelope.signature
    except (AttributeError, TypeError):
        raise MemoryRuntimeError("authorization_invalid") from None
    if (
        not _valid_binding_shape(binding)
        or binding != expected_binding
        or not _valid_action_id(action_id)
        or type(signature) is not bytes
        or len(signature) != _ACTION_SIGNATURE_BYTES
        or not _valid_monotonic_ns(issued_at_ns)
        or not _valid_monotonic_ns(expires_at_ns)
        or expires_at_ns <= issued_at_ns
        or expires_at_ns - issued_at_ns > ACTION_CAPABILITY_TTL_NS
    ):
        raise MemoryRuntimeError("authorization_invalid")
    try:
        payload = _binding_payload(
            action_id=action_id,
            binding=binding,
            issued_at_ns=issued_at_ns,
            expires_at_ns=expires_at_ns,
        )
    except (TypeError, ValueError, UnicodeError):
        raise MemoryRuntimeError("authorization_invalid") from None
    expected_signature = hmac.new(
        authority._action_secret,
        payload,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(signature, expected_signature):
        raise MemoryRuntimeError("authorization_invalid")
    current_ns = time.monotonic_ns()
    if not _valid_monotonic_ns(current_ns):
        raise MemoryRuntimeError("authorization_invalid")
    if current_ns < issued_at_ns:
        raise MemoryRuntimeError("authorization_not_yet_valid")
    if current_ns > expires_at_ns:
        raise MemoryRuntimeError("authorization_expired")
    if (
        current_ns - issued_at_ns > ACTION_CAPABILITY_TTL_NS
        or expires_at_ns - current_ns > ACTION_CAPABILITY_TTL_NS
    ):
        raise MemoryRuntimeError("authorization_invalid")
    with authority._action_lock:
        if (
            action_id in authority._inflight_actions
            or action_id in authority._consumed_actions
        ):
            raise MemoryRuntimeError("authorization_replayed")
        authority._inflight_actions.add(action_id)
    return action_id


def finish_action_consumption(
    authority: object,
    action_id: str,
    *,
    consumed: bool,
) -> None:
    require_runtime_authority(authority)
    with authority._action_lock:
        authority._inflight_actions.discard(action_id)
        if consumed:
            authority._consumed_actions.add(action_id)


def bootstrap_memory_runtime_from_environment(telegram_config) -> MemoryRuntime:
    """Create the process's only Memory runtime from the formal deployment loader."""
    global _PROCESS_AUTHORITY, _PROCESS_BOOTSTRAPPED
    with _BOOTSTRAP_LOCK:
        if _PROCESS_BOOTSTRAPPED:
            raise MemoryRuntimeError("memory_runtime_already_initialized")
        deployment = deployment_config.load_deployment_config(telegram_config)
        policy = _policy_from_config(deployment.memory)
        authority = _RuntimeAuthority(
            _AUTHORITY_CONSTRUCTOR_TOKEN,
            identity=_PROCESS_RUNTIME_IDENTITY,
            policy=policy,
            action_secret=secrets.token_bytes(32),
        )
        _PROCESS_AUTHORITY = authority
        try:
            from . import memory_service, memory_store
        except ImportError:
            import memory_service
            import memory_store
        try:
            path = str(Path(deployment.db_path))
            store = memory_store.MemoryStore(path, authority)
            expected_profile = (
                store._profile_parameters()
                if policy.explicit_writes_enabled and policy.configuration_valid
                else None
            )
            reader = memory_store.MemoryReader(
                path,
                expected_profile=expected_profile,
            )
            read_service = memory_service.MemoryReadService(
                reader,
                enabled=policy.enabled,
                configuration_valid=policy.configuration_valid,
                error_category=policy.error_category,
                explicit_writes_enabled=policy.explicit_writes_enabled,
                policy=store.policy,
            )
            privileged_actions = memory_service.PrivilegedMemoryActions(store, authority)
            runtime = MemoryRuntime(
                read_service=read_service,
                privileged_actions=privileged_actions,
            )
        except Exception:
            _PROCESS_AUTHORITY = None
            raise
        _PROCESS_BOOTSTRAPPED = True
        return runtime
