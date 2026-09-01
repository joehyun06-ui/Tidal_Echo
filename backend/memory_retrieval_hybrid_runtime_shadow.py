"""Default-off per-query runtime hook for Phase 4D-D3B Hybrid Retrieval shadow.

The existing synchronous Memory context preparation remains the sole
provider-visible authority. Only its hidden exact selected Memory keys plus the
already-validated user query are handed to one best-effort asynchronous shadow.
D3B3 adds only process-local structural observability; it never changes prompt
context, Memory truth, readiness, or retrieval authority.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from contextlib import asynccontextmanager
from typing import Final

from backend import (
    deployment_config,
    memory_retrieval_hybrid_observability,
    memory_retrieval_hybrid_query,
    memory_retrieval_hybrid_shadow,
)


ENV_GATE: Final = "MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED"
INSTALL_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED"
LOOP_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_LOOP"
TASK_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_TASK"
ORIGINAL_PREPARE_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_ORIGINAL_PREPARE"
OBSERVABILITY_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_OBSERVABILITY"


class MemoryHybridRetrievalRuntimeShadowError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if category in {
                "memory_hybrid_retrieval_shadow_configuration_invalid",
                "memory_hybrid_retrieval_shadow_requires_memory_context",
                "memory_hybrid_retrieval_shadow_runner_missing",
            }
            else "memory_hybrid_retrieval_shadow_configuration_invalid"
        )
        self.category = safe
        super().__init__(safe)


def enabled_from_environment(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return deployment_config.parse_strict_bool(
        env.get(ENV_GATE, "false"),
        "invalid_memory_hybrid_retrieval_shadow_enabled",
    )


def _log_line(line: str) -> None:
    try:
        print(line, file=sys.stderr, flush=True)
    except BaseException:
        pass


def _log_report(report: object) -> None:
    try:
        line = memory_retrieval_hybrid_shadow.render_hybrid_retrieval_shadow_telemetry_v1(
            report
        )
        if line is not None:
            _log_line(line)
    except BaseException:
        pass


def _log_skipped(reason: str) -> None:
    if reason not in {"busy", "authority_keys_unavailable", "loop_unavailable"}:
        reason = "shadow_unavailable"
    _log_line(
        "[memory-hybrid-retrieval-shadow] "
        f"status=skipped reason={reason}"
    )


def _record_attempt(tracker: object) -> None:
    try:
        tracker.record_attempt()
    except BaseException:
        pass


def _record_started(tracker: object) -> None:
    try:
        tracker.record_started()
    except BaseException:
        pass


def _record_skipped(tracker: object, reason: str) -> None:
    try:
        tracker.record_skipped(reason)
    except BaseException:
        pass


def _record_cancelled(tracker: object) -> None:
    try:
        tracker.record_cancelled()
    except BaseException:
        pass


def _record_report(tracker: object, report: object) -> None:
    try:
        tracker.record_report(report)
    except BaseException:
        pass


async def _run_shadow(
    relay_app: object,
    runner: object,
    tracker: object,
    *,
    query_text: str,
    authoritative_memory_keys: tuple[str, ...],
) -> None:
    try:
        produced = runner(query_text=query_text)
        if not inspect.isawaitable(produced):
            raise MemoryHybridRetrievalRuntimeShadowError(
                "memory_hybrid_retrieval_shadow_configuration_invalid"
            )
        hybrid_result = await produced
        if type(hybrid_result) is not memory_retrieval_hybrid_query.HybridQueryResultV1:
            raise MemoryHybridRetrievalRuntimeShadowError(
                "memory_hybrid_retrieval_shadow_configuration_invalid"
            )
        report = memory_retrieval_hybrid_shadow.compare_hybrid_retrieval_shadow_v1(
            authoritative_memory_keys,
            hybrid_result,
        )
        _record_report(tracker, report)
        _log_report(report)
    except asyncio.CancelledError:
        _record_cancelled(tracker)
        raise
    except BaseException:
        report = memory_retrieval_hybrid_shadow.HybridRetrievalShadowReportV1.failed()
        _record_report(tracker, report)
        _log_report(report)


def _spawn_shadow(
    relay_app: object,
    runner: object,
    tracker: object,
    query_text: str,
    authoritative_memory_keys: tuple[str, ...],
) -> None:
    try:
        active = getattr(relay_app, TASK_MARKER, None)
        if active is not None and not active.done():
            _record_skipped(tracker, "busy")
            _log_skipped("busy")
            return
        task = asyncio.create_task(
            _run_shadow(
                relay_app,
                runner,
                tracker,
                query_text=query_text,
                authoritative_memory_keys=authoritative_memory_keys,
            )
        )
        _record_started(tracker)
        setattr(relay_app, TASK_MARKER, task)

        def clear(completed: asyncio.Task) -> None:
            try:
                if getattr(relay_app, TASK_MARKER, None) is completed:
                    setattr(relay_app, TASK_MARKER, None)
            except BaseException:
                pass

        task.add_done_callback(clear)
    except BaseException:
        report = memory_retrieval_hybrid_shadow.HybridRetrievalShadowReportV1.failed()
        _record_report(tracker, report)
        _log_report(report)


def _submit_shadow_from_worker(
    relay_app: object,
    runner: object,
    tracker: object,
    *,
    query_text: object,
    authoritative_memory_keys: object,
) -> None:
    try:
        if (
            type(query_text) is not str
            or not query_text.strip()
            or type(authoritative_memory_keys) is not tuple
        ):
            _record_skipped(tracker, "authority_keys_unavailable")
            _log_skipped("authority_keys_unavailable")
            return
        loop = getattr(relay_app, LOOP_MARKER, None)
        if not isinstance(loop, asyncio.AbstractEventLoop) or loop.is_closed():
            _record_skipped(tracker, "loop_unavailable")
            _log_skipped("loop_unavailable")
            return
        keys = tuple(authoritative_memory_keys)
        loop.call_soon_threadsafe(
            _spawn_shadow,
            relay_app,
            runner,
            tracker,
            query_text,
            keys,
        )
    except BaseException:
        report = memory_retrieval_hybrid_shadow.HybridRetrievalShadowReportV1.failed()
        _record_report(tracker, report)
        _log_report(report)


def status_payload_v1(relay_app: object) -> dict:
    """Return process-local structural shadow status; never inspect Memory content."""

    try:
        installed = bool(getattr(relay_app, INSTALL_MARKER, False))
        enabled = bool(getattr(relay_app, ENABLED_MARKER, False))
        task = getattr(relay_app, TASK_MARKER, None)
        in_flight = bool(task is not None and not task.done())
        tracker = getattr(relay_app, OBSERVABILITY_MARKER, None)
        available = type(tracker) is memory_retrieval_hybrid_observability.HybridShadowObservabilityV1
        if not available:
            tracker = memory_retrieval_hybrid_observability.HybridShadowObservabilityV1()
        snapshot = tracker.snapshot()
        return memory_retrieval_hybrid_observability.project_status_payload_v1(
            snapshot,
            enabled=enabled,
            installed=installed,
            in_flight=in_flight,
            observability_available=(available or not enabled),
        )
    except BaseException:
        tracker = memory_retrieval_hybrid_observability.HybridShadowObservabilityV1()
        return memory_retrieval_hybrid_observability.project_status_payload_v1(
            tracker.snapshot(),
            enabled=False,
            installed=False,
            in_flight=False,
            observability_available=False,
        )


def install(relay_app: object, *, runner: object = None) -> bool:
    """Install a busy-drop shadow hook; gate OFF leaves callables/lifespan unchanged."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))

    enabled = enabled_from_environment(os.environ)
    if not enabled:
        setattr(relay_app, INSTALL_MARKER, True)
        setattr(relay_app, ENABLED_MARKER, False)
        return False

    try:
        memory = relay_app.DEPLOYMENT.memory
        if (
            not memory.enabled
            or not memory.configuration_valid
            or not memory.context_injection_enabled
            or not memory.smart_retrieval_enabled
        ):
            raise MemoryHybridRetrievalRuntimeShadowError(
                "memory_hybrid_retrieval_shadow_requires_memory_context"
            )
        if not callable(runner):
            raise MemoryHybridRetrievalRuntimeShadowError(
                "memory_hybrid_retrieval_shadow_runner_missing"
            )
        context_module = relay_app.memory_context_integration
        original_prepare = context_module.prepare_transient_memory_dispatch
        if not callable(original_prepare):
            raise MemoryHybridRetrievalRuntimeShadowError(
                "memory_hybrid_retrieval_shadow_configuration_invalid"
            )
        app = relay_app.app
        original_lifespan = app.router.lifespan_context
        tracker = memory_retrieval_hybrid_observability.HybridShadowObservabilityV1()
    except MemoryHybridRetrievalRuntimeShadowError:
        raise
    except BaseException:
        raise MemoryHybridRetrievalRuntimeShadowError(
            "memory_hybrid_retrieval_shadow_configuration_invalid"
        ) from None

    setattr(relay_app, ORIGINAL_PREPARE_MARKER, original_prepare)

    def shadow_prepare(read_service, base_messages, **kwargs):
        dispatch = original_prepare(read_service, base_messages, **kwargs)
        _record_attempt(tracker)
        try:
            keys = object.__getattribute__(dispatch, "authoritative_memory_keys")
            if keys is None:
                _record_skipped(tracker, "authority_keys_unavailable")
                _log_skipped("authority_keys_unavailable")
            else:
                query_text = base_messages[-1]["content"]
                _submit_shadow_from_worker(
                    relay_app,
                    runner,
                    tracker,
                    query_text=query_text,
                    authoritative_memory_keys=keys,
                )
        except BaseException:
            report = memory_retrieval_hybrid_shadow.HybridRetrievalShadowReportV1.failed()
            _record_report(tracker, report)
            _log_report(report)
        return dispatch

    context_module.prepare_transient_memory_dispatch = shadow_prepare

    @asynccontextmanager
    async def shadow_lifespan(application):
        async with original_lifespan(application):
            setattr(relay_app, LOOP_MARKER, asyncio.get_running_loop())
            setattr(relay_app, TASK_MARKER, None)
            try:
                yield
            finally:
                task = getattr(relay_app, TASK_MARKER, None)
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except BaseException:
                        pass
                setattr(relay_app, TASK_MARKER, None)
                setattr(relay_app, LOOP_MARKER, None)

    app.router.lifespan_context = shadow_lifespan
    # Commit state last: failed installation leaves no observability marker and
    # no enabled/install marker, preserving D3B1's installation atomicity.
    setattr(relay_app, OBSERVABILITY_MARKER, tracker)
    setattr(relay_app, ENABLED_MARKER, True)
    setattr(relay_app, INSTALL_MARKER, True)
    return True


__all__ = (
    "ENV_GATE",
    "MemoryHybridRetrievalRuntimeShadowError",
    "enabled_from_environment",
    "install",
    "status_payload_v1",
)
