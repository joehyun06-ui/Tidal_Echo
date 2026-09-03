"""Default-off provider-visible Hybrid Retrieval runtime for Phase 4D-D3C2.

D3C2 promotes the reviewed D3C1 same-revision selection contract to a runtime
surface, but keeps it repository-default OFF.  The active path is deliberately
mutually exclusive with Hybrid shadow and Memory Retrieval V2 shadow/active.

The relay's existing synchronous Memory preparation runs in ``asyncio.to_thread``.
Network embedding must not be hidden inside that worker because client
cancellation would not propagate.  When the active gate is ON this module
therefore patches two server-owned callables together:

* synchronous Memory preparation validates the normal context call and inserts
  one fixed internal developer-message sentinel instead of running the old
  selector;
* the async Kelivo generator wrapper consumes that sentinel, runs D3C1 under a
  bounded timeout on the request task, replaces the sentinel with the existing
  Memory developer-message envelope, then calls the original generator.

The sentinel is never passed to the provider.  Hybrid failure never falls back
to the pre-existing selector: the request fails closed as
``memory_context_unavailable``.  Canonical Memory truth and disposable sidecars
remain unchanged in authority.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Final, Mapping

from backend import (
    deployment_config,
    kelivo_service,
    memory_context_integration,
    memory_retrieval_hybrid_active,
    memory_retrieval_hybrid_runtime_composition,
    memory_retrieval_hybrid_runtime_shadow,
)


ACTIVE_RUNTIME_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-active-runtime-v1"
ENV_GATE: Final = "MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED"
ACTIVE_RETRIEVAL_TIMEOUT_SECONDS: Final = 60.0
PENDING_SENTINEL_VERSION: Final = "memory-hybrid-active-pending/v1"
PENDING_SENTINEL_CONTENT: Final = '{"version":"memory-hybrid-active-pending/v1"}'

INSTALL_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_ACTIVE_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_ACTIVE_ENABLED"
TRACKER_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_ACTIVE_OBSERVABILITY"
ORIGINAL_PREPARE_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_ACTIVE_ORIGINAL_PREPARE"
ORIGINAL_GENERATOR_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_ACTIVE_ORIGINAL_GENERATOR"

_MAX_COUNTER: Final = 1_000_000
_MAX_DURATION_MS: Final = 86_400_000
_FAILURE_CATEGORIES: Final = frozenset({
    "hybrid_active_channels_unavailable",
    "hybrid_active_configuration_invalid",
    "hybrid_active_query_failed",
    "hybrid_active_render_failed",
    "hybrid_active_selection_invalid",
    "hybrid_active_stale",
    "memory_context_unavailable",
})
_RUNTIME_ERROR_CATEGORIES: Final = frozenset({
    "memory_hybrid_active_configuration_invalid",
    "memory_hybrid_active_conflicts_shadow",
    "memory_hybrid_active_conflicts_v2",
    "memory_hybrid_active_generator_missing",
    "memory_hybrid_active_requires_memory_context",
    "memory_hybrid_active_runner_missing",
})


class MemoryHybridRetrievalRuntimeActiveError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _RUNTIME_ERROR_CATEGORIES
            else "memory_hybrid_active_configuration_invalid"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except BaseException:
            return "memory_hybrid_active_configuration_invalid"

    def __repr__(self) -> str:
        return f"MemoryHybridRetrievalRuntimeActiveError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryHybridRetrievalRuntimeActiveError(category)


def enabled_from_environment(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return deployment_config.parse_strict_bool(
        env.get(ENV_GATE, "false"),
        "invalid_memory_hybrid_retrieval_active_enabled",
    )


def _bounded_increment(value: int) -> int:
    return min(_MAX_COUNTER, max(0, value) + 1)


def _bounded_duration_ms(value: object) -> int:
    try:
        milliseconds = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(_MAX_DURATION_MS, max(0, milliseconds))


def _safe_failure_category(value: object) -> str:
    return (
        value
        if type(value) is str and value in _FAILURE_CATEGORIES
        else "memory_context_unavailable"
    )


class HybridActiveObservabilityV1:
    """Bounded process-local structural telemetry; stores no Memory/query text."""

    __slots__ = (
        "_lock",
        "attempts",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "in_flight",
        "latest_duration_ms",
        "max_duration_ms",
        "total_duration_ms",
        "last_status",
        "last_selected_count",
        "last_total_chars",
        "last_query_embedding_performed",
        "last_failure_category",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.attempts = 0
        self.completed = 0
        self.failed = 0
        self.timed_out = 0
        self.cancelled = 0
        self.in_flight = 0
        self.latest_duration_ms = 0
        self.max_duration_ms = 0
        self.total_duration_ms = 0
        self.last_status = "none"
        self.last_selected_count = 0
        self.last_total_chars = 0
        self.last_query_embedding_performed = False
        self.last_failure_category = ""

    def _finish_duration(self, duration_ms: object) -> int:
        duration = _bounded_duration_ms(duration_ms)
        self.latest_duration_ms = duration
        self.max_duration_ms = max(self.max_duration_ms, duration)
        self.total_duration_ms = min(
            _MAX_DURATION_MS * _MAX_COUNTER,
            self.total_duration_ms + duration,
        )
        self.in_flight = max(0, self.in_flight - 1)
        return duration

    def record_attempt(self) -> None:
        with self._lock:
            self.attempts = _bounded_increment(self.attempts)
            self.in_flight = min(_MAX_COUNTER, self.in_flight + 1)

    def record_completed(self, selection: object, duration_ms: object) -> None:
        try:
            selected_count = object.__getattribute__(selection, "selected_count")
            total_chars = object.__getattribute__(selection, "total_chars")
            embedding = object.__getattribute__(selection, "query_embedding_performed")
            if (
                type(selected_count) is not int
                or not 0 <= selected_count <= memory_retrieval_hybrid_active.ACTIVE_MAX_ITEMS
                or type(total_chars) is not int
                or not 0 <= total_chars <= memory_retrieval_hybrid_active.ACTIVE_CHARACTER_BUDGET
                or type(embedding) is not bool
            ):
                raise ValueError("invalid active selection telemetry")
        except BaseException:
            self.record_failed("memory_context_unavailable", duration_ms)
            return
        with self._lock:
            self.completed = _bounded_increment(self.completed)
            self._finish_duration(duration_ms)
            self.last_status = "completed"
            self.last_selected_count = selected_count
            self.last_total_chars = total_chars
            self.last_query_embedding_performed = embedding
            self.last_failure_category = ""

    def record_failed(self, category: object, duration_ms: object) -> None:
        with self._lock:
            self.failed = _bounded_increment(self.failed)
            self._finish_duration(duration_ms)
            self.last_status = "failed"
            self.last_selected_count = 0
            self.last_total_chars = 0
            self.last_query_embedding_performed = False
            self.last_failure_category = _safe_failure_category(category)

    def record_timed_out(self, duration_ms: object) -> None:
        with self._lock:
            self.timed_out = _bounded_increment(self.timed_out)
            self._finish_duration(duration_ms)
            self.last_status = "timed_out"
            self.last_selected_count = 0
            self.last_total_chars = 0
            self.last_query_embedding_performed = False
            self.last_failure_category = "memory_context_unavailable"

    def record_cancelled(self, duration_ms: object) -> None:
        with self._lock:
            self.cancelled = _bounded_increment(self.cancelled)
            self._finish_duration(duration_ms)
            self.last_status = "cancelled"
            self.last_selected_count = 0
            self.last_total_chars = 0
            self.last_query_embedding_performed = False
            self.last_failure_category = ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "attempts": self.attempts,
                "completed": self.completed,
                "failed": self.failed,
                "timed_out": self.timed_out,
                "cancelled": self.cancelled,
                "in_flight": self.in_flight,
                "latest_duration_ms": self.latest_duration_ms,
                "max_duration_ms": self.max_duration_ms,
                "total_duration_ms": self.total_duration_ms,
                "last_status": self.last_status,
                "last_selected_count": self.last_selected_count,
                "last_total_chars": self.last_total_chars,
                "last_query_embedding_performed": self.last_query_embedding_performed,
                "last_failure_category": self.last_failure_category,
            }


def _elapsed_ms(started: float) -> int:
    try:
        return _bounded_duration_ms((time.monotonic() - started) * 1000.0)
    except BaseException:
        return 0


def _pending_message() -> dict[str, str]:
    return {"role": "developer", "content": PENDING_SENTINEL_CONTENT}


def _is_pending_message(message: object) -> bool:
    return (
        type(message) is dict
        and set(message) == {"role", "content"}
        and message.get("role") == "developer"
        and message.get("content") == PENDING_SENTINEL_CONTENT
    )


def _compose_runner(
    relay_app: object,
    environ: Mapping[str, str],
) -> memory_retrieval_hybrid_runtime_composition.HybridRetrievalShadowRunnerV1:
    """Reuse the exact D3B2 configuration contract without changing process env."""

    try:
        projected = dict(environ)
        # D3B2's public loader is shadow-gated.  For the active runtime, project
        # only that internal loader gate in a private mapping; the real process
        # environment remains shadow=false and active=true.
        projected[memory_retrieval_hybrid_runtime_shadow.ENV_GATE] = "true"
        runner = (
            memory_retrieval_hybrid_runtime_composition
            .compose_hybrid_retrieval_shadow_runner_v1(relay_app, projected)
        )
        if type(runner) is not (
            memory_retrieval_hybrid_runtime_composition
            .HybridRetrievalShadowRunnerV1
        ):
            _raise("memory_hybrid_active_runner_missing")
        return runner
    except MemoryHybridRetrievalRuntimeActiveError:
        raise
    except BaseException:
        _raise("memory_hybrid_active_configuration_invalid")


def _validate_runtime_requirements(relay_app: object, environ: Mapping[str, str]) -> None:
    try:
        if memory_retrieval_hybrid_runtime_shadow.enabled_from_environment(environ):
            _raise("memory_hybrid_active_conflicts_shadow")
        if bool(
            getattr(
                relay_app,
                memory_retrieval_hybrid_runtime_shadow.ENABLED_MARKER,
                False,
            )
        ):
            _raise("memory_hybrid_active_conflicts_shadow")

        memory = relay_app.DEPLOYMENT.memory
        if (
            not memory.enabled
            or not memory.configuration_valid
            or not memory.context_injection_enabled
            or not memory.smart_retrieval_enabled
        ):
            _raise("memory_hybrid_active_requires_memory_context")
        if (
            bool(memory.retrieval_v2_shadow_enabled)
            or bool(memory.retrieval_v2_active_enabled)
        ):
            _raise("memory_hybrid_active_conflicts_v2")
    except MemoryHybridRetrievalRuntimeActiveError:
        raise
    except BaseException:
        _raise("memory_hybrid_active_configuration_invalid")


def _validate_rendered_developer_message(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if (
        type(value) is not dict
        or set(value) != {"role", "content"}
        or value.get("role") != "developer"
        or type(value.get("content")) is not str
        or not value["content"]
        or len(value["content"]) > memory_context_integration.MAX_CONTENT_CHARS
    ):
        raise memory_context_integration.MemoryContextIntegrationError()
    try:
        value["content"].encode("utf-8", errors="strict")
    except UnicodeError:
        raise memory_context_integration.MemoryContextIntegrationError() from None
    return value


def status_payload_v1(relay_app: object) -> dict:
    """Authenticated callers receive only bounded structural active-path state."""

    try:
        installed = bool(getattr(relay_app, INSTALL_MARKER, False))
        enabled = bool(getattr(relay_app, ENABLED_MARKER, False))
        tracker = getattr(relay_app, TRACKER_MARKER, None)
        available = type(tracker) is HybridActiveObservabilityV1
        if not available:
            tracker = HybridActiveObservabilityV1()
        snapshot = tracker.snapshot()
        return {
            "contract_version": ACTIVE_RUNTIME_CONTRACT_VERSION,
            "enabled": enabled,
            "installed": installed,
            "observability_available": (available or not enabled),
            "timeout_seconds": ACTIVE_RETRIEVAL_TIMEOUT_SECONDS,
            "in_flight": snapshot["in_flight"],
            "attempts": snapshot["attempts"],
            "outcomes": {
                "completed": snapshot["completed"],
                "failed": snapshot["failed"],
                "timed_out": snapshot["timed_out"],
                "cancelled": snapshot["cancelled"],
            },
            "latency_ms": {
                "latest": snapshot["latest_duration_ms"],
                "max": snapshot["max_duration_ms"],
                "total": snapshot["total_duration_ms"],
            },
            "last": {
                "status": snapshot["last_status"],
                "selected_count": snapshot["last_selected_count"],
                "total_chars": snapshot["last_total_chars"],
                "query_embedding_performed": snapshot[
                    "last_query_embedding_performed"
                ],
                "failure_category": snapshot["last_failure_category"],
            },
        }
    except BaseException:
        tracker = HybridActiveObservabilityV1()
        snapshot = tracker.snapshot()
        return {
            "contract_version": ACTIVE_RUNTIME_CONTRACT_VERSION,
            "enabled": False,
            "installed": False,
            "observability_available": False,
            "timeout_seconds": ACTIVE_RETRIEVAL_TIMEOUT_SECONDS,
            "in_flight": 0,
            "attempts": 0,
            "outcomes": {
                "completed": 0,
                "failed": 0,
                "timed_out": 0,
                "cancelled": 0,
            },
            "latency_ms": {"latest": 0, "max": 0, "total": 0},
            "last": {
                "status": snapshot["last_status"],
                "selected_count": 0,
                "total_chars": 0,
                "query_embedding_performed": False,
                "failure_category": "",
            },
        }


def install(
    relay_app: object,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Install active authority only when the dedicated strict gate is ON."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))

    env = os.environ if environ is None else environ
    enabled = enabled_from_environment(env)
    if not enabled:
        setattr(relay_app, INSTALL_MARKER, True)
        setattr(relay_app, ENABLED_MARKER, False)
        return False

    _validate_runtime_requirements(relay_app, env)

    try:
        context_module = relay_app.memory_context_integration
        original_prepare = context_module.prepare_transient_memory_dispatch
        original_generator = relay_app.KELIVO_GENERATOR
        if not callable(original_prepare):
            _raise("memory_hybrid_active_configuration_invalid")
        if not callable(original_generator):
            _raise("memory_hybrid_active_generator_missing")
        runner = _compose_runner(relay_app, env)
        tracker = HybridActiveObservabilityV1()
    except MemoryHybridRetrievalRuntimeActiveError:
        raise
    except BaseException:
        _raise("memory_hybrid_active_configuration_invalid")

    def active_prepare(
        read_service,
        base_messages,
        *,
        enabled: bool,
        smart_retrieval_enabled: bool,
        retrieval_v2_shadow_enabled: bool = False,
        retrieval_v2_active_enabled: bool = False,
    ):
        del read_service
        if (
            enabled is not True
            or smart_retrieval_enabled is not True
            or retrieval_v2_shadow_enabled is not False
            or retrieval_v2_active_enabled is not False
        ):
            raise memory_context_integration.MemoryContextIntegrationError()
        messages = memory_context_integration._validate_base_messages(base_messages)
        if any(_is_pending_message(message) for message in messages):
            raise memory_context_integration.MemoryContextIntegrationError()
        provider_messages = (
            *messages[:-1],
            _pending_message(),
            messages[-1],
        )
        if len(provider_messages) > memory_context_integration.TRANSIENT_DISPATCH_MAX_MESSAGES:
            raise memory_context_integration.MemoryContextIntegrationError()
        return memory_context_integration.TransientMemoryDispatch(
            provider_messages=provider_messages,
            memory_applied=False,
            retrieval_v2_shadow_report=None,
            retrieval_v2_active_report=None,
            authoritative_memory_keys=(),
        )

    async def active_generator(
        messages,
        api_session,
        provider_model,
        temperature,
        max_tokens,
        context,
    ):
        try:
            pending_indexes = tuple(
                index
                for index, message in enumerate(messages)
                if _is_pending_message(message)
            )
        except BaseException:
            pending_indexes = ()

        # Other server-owned generator call paths remain byte-for-byte delegated.
        if not pending_indexes:
            return await original_generator(
                messages,
                api_session,
                provider_model,
                temperature,
                max_tokens,
                context,
            )

        if (
            type(messages) is not tuple
            or len(pending_indexes) != 1
            or pending_indexes[0] != len(messages) - 2
            or type(context) is not dict
            or "transient_memory_dispatch" in context
        ):
            raise memory_context_integration.MemoryContextIntegrationError()

        base_messages = tuple(
            message
            for index, message in enumerate(messages)
            if index != pending_indexes[0]
        )
        base_messages = memory_context_integration._validate_base_messages(
            base_messages
        )
        query_text = base_messages[-1]["content"]
        tracker.record_attempt()
        started = time.monotonic()

        try:
            async with asyncio.timeout(ACTIVE_RETRIEVAL_TIMEOUT_SECONDS):
                selection = await (
                    memory_retrieval_hybrid_active
                    .plan_hybrid_active_selection_v1(
                        runner,
                        query_text=query_text,
                    )
                )
                developer_message = (
                    memory_retrieval_hybrid_active
                    .render_hybrid_active_developer_message_v1(selection)
                )
                developer_message = _validate_rendered_developer_message(
                    developer_message
                )
        except asyncio.CancelledError:
            tracker.record_cancelled(_elapsed_ms(started))
            raise
        except TimeoutError:
            tracker.record_timed_out(_elapsed_ms(started))
            raise memory_context_integration.MemoryContextIntegrationError() from None
        except memory_retrieval_hybrid_active.MemoryRetrievalHybridActiveError as error:
            tracker.record_failed(error.category, _elapsed_ms(started))
            raise memory_context_integration.MemoryContextIntegrationError() from None
        except memory_context_integration.MemoryContextIntegrationError:
            tracker.record_failed("memory_context_unavailable", _elapsed_ms(started))
            raise
        except Exception:
            tracker.record_failed("memory_context_unavailable", _elapsed_ms(started))
            raise memory_context_integration.MemoryContextIntegrationError() from None

        if developer_message is None:
            provider_messages = base_messages
            next_context = dict(context)
        else:
            provider_messages = (
                *base_messages[:-1],
                dict(developer_message),
                base_messages[-1],
            )
            if (
                len(provider_messages)
                > memory_context_integration.TRANSIENT_DISPATCH_MAX_MESSAGES
            ):
                tracker.record_failed(
                    "memory_context_unavailable",
                    _elapsed_ms(started),
                )
                raise memory_context_integration.MemoryContextIntegrationError()
            next_context = dict(context)
            next_context["transient_memory_dispatch"] = (
                kelivo_service.TRANSIENT_MEMORY_DISPATCH_VERSION
            )

        tracker.record_completed(selection, _elapsed_ms(started))
        return await original_generator(
            provider_messages,
            api_session,
            provider_model,
            temperature,
            max_tokens,
            next_context,
        )

    # Two callables form one authority switch.  Roll back the first assignment if
    # the second cannot be committed; install/enabled markers are written last.
    setattr(relay_app, ORIGINAL_PREPARE_MARKER, original_prepare)
    setattr(relay_app, ORIGINAL_GENERATOR_MARKER, original_generator)
    try:
        context_module.prepare_transient_memory_dispatch = active_prepare
        relay_app.KELIVO_GENERATOR = active_generator
    except BaseException:
        try:
            context_module.prepare_transient_memory_dispatch = original_prepare
            relay_app.KELIVO_GENERATOR = original_generator
        except BaseException:
            pass
        _raise("memory_hybrid_active_configuration_invalid")

    setattr(relay_app, TRACKER_MARKER, tracker)
    setattr(relay_app, ENABLED_MARKER, True)
    setattr(relay_app, INSTALL_MARKER, True)
    return True


__all__ = (
    "ACTIVE_RETRIEVAL_TIMEOUT_SECONDS",
    "ACTIVE_RUNTIME_CONTRACT_VERSION",
    "ENV_GATE",
    "HybridActiveObservabilityV1",
    "MemoryHybridRetrievalRuntimeActiveError",
    "PENDING_SENTINEL_VERSION",
    "enabled_from_environment",
    "install",
    "status_payload_v1",
)
