"""Default-off per-query runtime hook for Phase 4D-D3B1.

D3B1 installs a non-authoritative shadow hook around the existing synchronous
Memory context preparation boundary.  The original context dispatch completes
first and remains the sole provider-visible authority.  Only its hidden exact
selected Memory keys plus the already-validated user query are handed back to
the main event loop for one best-effort asynchronous shadow comparison.

The hook owns no retrieval resources yet.  A later D3B2 composition supplies the
server-owned hybrid query runner (sidecars, term secret, embedding adapter).  If
this gate is enabled without that runner, startup fails closed rather than
silently running a partial pseudo-hybrid configuration.
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
    memory_retrieval_hybrid_query,
    memory_retrieval_hybrid_shadow,
)


ENV_GATE: Final = "MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED"
INSTALL_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_ENABLED"
LOOP_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_LOOP"
TASK_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_SHADOW_TASK"
ORIGINAL_PREPARE_MARKER: Final = "_MEMORY_HYBRID_RETRIEVAL_ORIGINAL_PREPARE"


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


async def _run_shadow(
    relay_app: object,
    runner: object,
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
        report = (
            memory_retrieval_hybrid_shadow.compare_hybrid_retrieval_shadow_v1(
                authoritative_memory_keys,
                hybrid_result,
            )
        )
        _log_report(report)
    except asyncio.CancelledError:
        raise
    except BaseException:
        _log_report(memory_retrieval_hybrid_shadow.HybridRetrievalShadowReportV1.failed())


def _spawn_shadow(
    relay_app: object,
    runner: object,
    query_text: str,
    authoritative_memory_keys: tuple[str, ...],
) -> None:
    try:
        active = getattr(relay_app, TASK_MARKER, None)
        if active is not None and not active.done():
            _log_skipped("busy")
            return
        task = asyncio.create_task(
            _run_shadow(
                relay_app,
                runner,
                query_text=query_text,
                authoritative_memory_keys=authoritative_memory_keys,
            )
        )
        setattr(relay_app, TASK_MARKER, task)

        def clear(completed: asyncio.Task) -> None:
            try:
                if getattr(relay_app, TASK_MARKER, None) is completed:
                    setattr(relay_app, TASK_MARKER, None)
            except BaseException:
                pass

        task.add_done_callback(clear)
    except BaseException:
        _log_report(memory_retrieval_hybrid_shadow.HybridRetrievalShadowReportV1.failed())


def _submit_shadow_from_worker(
    relay_app: object,
    runner: object,
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
            _log_skipped("authority_keys_unavailable")
            return
        loop = getattr(relay_app, LOOP_MARKER, None)
        if not isinstance(loop, asyncio.AbstractEventLoop) or loop.is_closed():
            _log_skipped("loop_unavailable")
            return
        keys = tuple(authoritative_memory_keys)
        loop.call_soon_threadsafe(
            _spawn_shadow,
            relay_app,
            runner,
            query_text,
            keys,
        )
    except BaseException:
        _log_report(memory_retrieval_hybrid_shadow.HybridRetrievalShadowReportV1.failed())


def install(relay_app: object, *, runner: object = None) -> bool:
    """Install a busy-drop shadow hook; gate OFF leaves all callables unchanged."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))

    enabled = enabled_from_environment(os.environ)
    setattr(relay_app, INSTALL_MARKER, True)
    setattr(relay_app, ENABLED_MARKER, enabled)
    if not enabled:
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
    except MemoryHybridRetrievalRuntimeShadowError:
        raise
    except BaseException:
        raise MemoryHybridRetrievalRuntimeShadowError(
            "memory_hybrid_retrieval_shadow_configuration_invalid"
        ) from None

    setattr(relay_app, ORIGINAL_PREPARE_MARKER, original_prepare)

    def shadow_prepare(read_service, base_messages, **kwargs):
        dispatch = original_prepare(read_service, base_messages, **kwargs)
        try:
            keys = object.__getattribute__(dispatch, "authoritative_memory_keys")
            if keys is None:
                _log_skipped("authority_keys_unavailable")
            else:
                query_text = base_messages[-1]["content"]
                _submit_shadow_from_worker(
                    relay_app,
                    runner,
                    query_text=query_text,
                    authoritative_memory_keys=keys,
                )
        except BaseException:
            _log_report(memory_retrieval_hybrid_shadow.HybridRetrievalShadowReportV1.failed())
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
    return True


__all__ = (
    "ENV_GATE",
    "MemoryHybridRetrievalRuntimeShadowError",
    "enabled_from_environment",
    "install",
)
