"""Provider-wire lifecycle hardening for Hybrid Retrieval Active (D3C2.1).

D3C2 records Hybrid retrieval completion immediately before delegating to the
real Kelivo generator.  That is correct for retrieval-only timing, but it makes
provider-visible canary telemetry misleading when the downstream provider then
rejects or cancels the request: ``completed`` has already been committed.

This patch deliberately does not change Hybrid selection, Memory rendering,
provider messages, provider configuration, or canonical Memory authority.  It
wraps the already-installed Active generator and defers only the process-local
completion accounting until the provider call returns.  A task-local ContextVar
keeps bounded structural selection metrics; query text, Memory plaintext,
identifiers, vectors, provider payloads, and secrets are never retained.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Final

from backend import (
    memory_context_integration,
    memory_retrieval_hybrid_active,
    memory_retrieval_hybrid_runtime_active as runtime_active,
)


PROVIDER_WIRE_CONTRACT_VERSION: Final = "memory-retrieval-hybrid-provider-wire-v1"
INSTALL_MARKER: Final = "_MEMORY_HYBRID_PROVIDER_WIRE_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_HYBRID_PROVIDER_WIRE_ENABLED"
ORIGINAL_GENERATOR_MARKER: Final = "_MEMORY_HYBRID_PROVIDER_WIRE_ORIGINAL_GENERATOR"

_PROVIDER_FAILURE_FALLBACK: Final = "provider_generation_failed"
_ALLOWED_PROVIDER_FAILURE_CATEGORIES: Final = frozenset({
    "assistant_response_too_large",
    "empty_model_response",
    "generation_response_too_large",
    "incomplete_stream_response",
    "invalid_generation_response",
    "invalid_stream_response",
    "model_failed",
    "model_stream_interrupted",
    "model_timeout",
    "model_transport_uncertain",
    "model_unexpected_uncertain",
    "no_supported_model",
    "provider_contract_unavailable",
    "provider_explicit_rejection",
    "provider_model_mismatch",
    "provider_response_too_large",
    "provider_response_uncertain",
})

# The outer provider-wire wrapper places one mutable, task-local state object in
# this ContextVar.  It contains only a tracker object identity and bounded
# integer/boolean metrics.  No query or Memory value is stored here.
_REQUEST_STATE: contextvars.ContextVar[dict[str, object] | None] = (
    contextvars.ContextVar("memory_hybrid_active_provider_wire_state", default=None)
)
_ORIGINAL_RECORD_COMPLETED = runtime_active.HybridActiveObservabilityV1.record_completed
_RECORD_COMPLETED_PATCHED = False


def _elapsed_ms(started: float) -> int:
    try:
        return runtime_active._bounded_duration_ms((time.monotonic() - started) * 1000.0)
    except BaseException:
        return 0


def _selection_metrics(selection: object) -> tuple[int, int, bool] | None:
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
            return None
        return selected_count, total_chars, embedding
    except BaseException:
        return None


def _safe_provider_failure_category(error: object) -> str:
    try:
        category = object.__getattribute__(error, "category")
    except BaseException:
        category = ""
    return (
        category
        if type(category) is str and category in _ALLOWED_PROVIDER_FAILURE_CATEGORIES
        else _PROVIDER_FAILURE_FALLBACK
    )


def _deferred_record_completed(
    tracker: runtime_active.HybridActiveObservabilityV1,
    selection: object,
    duration_ms: object,
) -> None:
    state = _REQUEST_STATE.get()
    if state is None or state.get("tracker_id") != id(tracker):
        _ORIGINAL_RECORD_COMPLETED(tracker, selection, duration_ms)
        return

    metrics = _selection_metrics(selection)
    if metrics is None or state.get("metrics") is not None:
        # Preserve D3C2's existing fail-safe behavior for malformed or duplicate
        # completion calls rather than inventing new authority semantics here.
        _ORIGINAL_RECORD_COMPLETED(tracker, selection, duration_ms)
        return
    state["metrics"] = metrics
    state["retrieval_duration_ms"] = runtime_active._bounded_duration_ms(duration_ms)


def _install_record_completed_patch() -> None:
    global _RECORD_COMPLETED_PATCHED
    if _RECORD_COMPLETED_PATCHED:
        return
    runtime_active.HybridActiveObservabilityV1.record_completed = _deferred_record_completed
    _RECORD_COMPLETED_PATCHED = True


def _preserve_selection_metrics(
    tracker: runtime_active.HybridActiveObservabilityV1,
    metrics: tuple[int, int, bool],
) -> None:
    selected_count, total_chars, embedding = metrics
    tracker.last_selected_count = selected_count
    tracker.last_total_chars = total_chars
    tracker.last_query_embedding_performed = embedding


def _finalize_completed(
    tracker: runtime_active.HybridActiveObservabilityV1,
    metrics: tuple[int, int, bool],
    duration_ms: object,
) -> None:
    with tracker._lock:
        tracker.completed = runtime_active._bounded_increment(tracker.completed)
        tracker._finish_duration(duration_ms)
        tracker.last_status = "completed"
        _preserve_selection_metrics(tracker, metrics)
        tracker.last_failure_category = ""


def _finalize_failed(
    tracker: runtime_active.HybridActiveObservabilityV1,
    metrics: tuple[int, int, bool],
    category: str,
    duration_ms: object,
) -> None:
    # Use the reviewed D3C2 accounting path for counters/in-flight first, then
    # replace only the bounded structural last-result fields under the same lock.
    tracker.record_failed("memory_context_unavailable", duration_ms)
    with tracker._lock:
        _preserve_selection_metrics(tracker, metrics)
        tracker.last_failure_category = category


def _finalize_cancelled(
    tracker: runtime_active.HybridActiveObservabilityV1,
    metrics: tuple[int, int, bool],
    duration_ms: object,
) -> None:
    tracker.record_cancelled(duration_ms)
    with tracker._lock:
        _preserve_selection_metrics(tracker, metrics)


def _log_provider_outcome(status: str, category: str = "") -> None:
    try:
        if status == "completed":
            print(
                "[memory-hybrid-active-provider-wire] "
                "status=completed stage=provider",
                flush=True,
            )
            return
        if status == "cancelled":
            print(
                "[memory-hybrid-active-provider-wire] "
                "status=cancelled stage=provider",
                flush=True,
            )
            return
        safe = (
            category
            if category in _ALLOWED_PROVIDER_FAILURE_CATEGORIES
            or category == _PROVIDER_FAILURE_FALLBACK
            else _PROVIDER_FAILURE_FALLBACK
        )
        print(
            "[memory-hybrid-active-provider-wire] "
            f"status=failed stage=provider category={safe}",
            flush=True,
        )
    except BaseException:
        pass


def install(relay_app: object) -> bool:
    """Bind provider lifecycle accounting after the reviewed D3C2 install."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))

    active_installed = bool(getattr(relay_app, runtime_active.INSTALL_MARKER, False))
    active_enabled = bool(getattr(relay_app, runtime_active.ENABLED_MARKER, False))
    if not active_installed:
        raise runtime_active.MemoryHybridRetrievalRuntimeActiveError(
            "memory_hybrid_active_configuration_invalid"
        )
    if not active_enabled:
        setattr(relay_app, INSTALL_MARKER, True)
        setattr(relay_app, ENABLED_MARKER, False)
        return False

    tracker = getattr(relay_app, runtime_active.TRACKER_MARKER, None)
    original_generator = getattr(relay_app, "KELIVO_GENERATOR", None)
    if (
        type(tracker) is not runtime_active.HybridActiveObservabilityV1
        or not callable(original_generator)
    ):
        raise runtime_active.MemoryHybridRetrievalRuntimeActiveError(
            "memory_hybrid_active_configuration_invalid"
        )

    _install_record_completed_patch()

    async def provider_wire_generator(*args, **kwargs):
        state: dict[str, object] = {
            "tracker_id": id(tracker),
            "metrics": None,
            "retrieval_duration_ms": 0,
        }
        token = _REQUEST_STATE.set(state)
        started = time.monotonic()
        try:
            result = await original_generator(*args, **kwargs)
        except asyncio.CancelledError:
            metrics = state.get("metrics")
            if type(metrics) is tuple and len(metrics) == 3:
                _finalize_cancelled(tracker, metrics, _elapsed_ms(started))
                _log_provider_outcome("cancelled")
            raise
        except Exception as error:
            metrics = state.get("metrics")
            if type(metrics) is tuple and len(metrics) == 3:
                category = _safe_provider_failure_category(error)
                _finalize_failed(tracker, metrics, category, _elapsed_ms(started))
                _log_provider_outcome("failed", category)
            raise
        else:
            metrics = state.get("metrics")
            if type(metrics) is tuple and len(metrics) == 3:
                _finalize_completed(tracker, metrics, _elapsed_ms(started))
                _log_provider_outcome("completed")
            return result
        finally:
            _REQUEST_STATE.reset(token)

    setattr(relay_app, ORIGINAL_GENERATOR_MARKER, original_generator)
    relay_app.KELIVO_GENERATOR = provider_wire_generator
    setattr(relay_app, ENABLED_MARKER, True)
    setattr(relay_app, INSTALL_MARKER, True)
    return True


__all__ = (
    "ENABLED_MARKER",
    "INSTALL_MARKER",
    "ORIGINAL_GENERATOR_MARKER",
    "PROVIDER_WIRE_CONTRACT_VERSION",
    "install",
)
