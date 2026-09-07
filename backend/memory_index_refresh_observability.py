"""Bounded, process-local index-worker telemetry; never an index health proof.

Only fixed categories, booleans and counts enter this module. It owns no database,
filesystem, provider, configuration, Memory authority or readiness capability.
The last completed receipt is historical, not a live outbox backlog measurement.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Final


OBSERVABILITY_CONTRACT_VERSION: Final = "memory-index-refresh-observability-v1"
MAX_COUNTER: Final = 1_000_000
MAX_BACKOFF_SECONDS: Final = 60
WORKER_ERROR_CATEGORIES: Final = frozenset({
    "memory_index_refresh_completion_failed",
    "memory_index_refresh_configuration_invalid",
    "memory_index_refresh_conflicts_runtime",
    "memory_index_refresh_outbox_read_failed",
    "memory_index_refresh_reconcile_failed",
    "memory_index_refresh_requires_memory",
    "memory_index_refresh_runner_invalid",
    "memory_index_refresh_worker_error",
})
_MODES: Final = frozenset({
    "disabled", "worker_only", "worker_readonly_shadow", "legacy_query_repair",
    "unavailable",
})
_TASK_STATES: Final = frozenset({
    "disabled", "not_running", "running", "done", "cancelled", "unavailable",
})
_LAST_STATUSES: Final = frozenset({
    "none", "running", "idle", "completed", "failed", "cancelled",
})
_RECEIPT_COUNTS: Final = (
    "batch_pending_count", "batch_completed_count", "source_atomic_count",
    "bm25_document_count", "vector_document_count", "provider_call_count",
)


def _invalid() -> None:
    raise ValueError("invalid_memory_index_refresh_observability")


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        _invalid()
    return min(MAX_COUNTER, value)


def _increment(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        _invalid()
    return min(MAX_COUNTER, value + 1)


@dataclass(frozen=True, slots=True, repr=False)
class IndexRefreshReceiptSummaryV1:
    batch_pending_count: int
    batch_completed_count: int
    rebuilt: bool
    source_atomic_count: int
    bm25_document_count: int
    vector_document_count: int
    provider_call_count: int

    def __post_init__(self) -> None:
        _validate_receipt(self)

    def __repr__(self) -> str:
        return "<IndexRefreshReceiptSummaryV1>"


def _validate_receipt(receipt: object) -> None:
    if type(receipt) is not IndexRefreshReceiptSummaryV1:
        _invalid()
    for name in _RECEIPT_COUNTS:
        value = getattr(receipt, name)
        if type(value) is not int or not 0 <= value <= MAX_COUNTER:
            _invalid()
    if type(receipt.rebuilt) is not bool:
        _invalid()
    if receipt.batch_completed_count > receipt.batch_pending_count:
        _invalid()


@dataclass(frozen=True, slots=True, repr=False)
class IndexRefreshObservabilitySnapshotV1:
    available: bool
    in_flight: bool
    attempt_count: int
    idle_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    consecutive_failures: int
    backoff_seconds: int
    last_status: str
    last_failure_category: str
    last_completed_receipt: IndexRefreshReceiptSummaryV1 | None

    def __post_init__(self) -> None:
        _validate_snapshot(self)

    def __repr__(self) -> str:
        return "<IndexRefreshObservabilitySnapshotV1>"


def _validate_snapshot(snapshot: object) -> None:
    if type(snapshot) is not IndexRefreshObservabilitySnapshotV1:
        _invalid()
    if type(snapshot.available) is not bool or type(snapshot.in_flight) is not bool:
        _invalid()
    for name in (
        "attempt_count", "idle_count", "completed_count", "failed_count",
        "cancelled_count", "consecutive_failures",
    ):
        value = getattr(snapshot, name)
        if type(value) is not int or not 0 <= value <= MAX_COUNTER:
            _invalid()
    if (
        type(snapshot.backoff_seconds) is not int
        or not 0 <= snapshot.backoff_seconds <= MAX_BACKOFF_SECONDS
    ):
        _invalid()
    if type(snapshot.last_status) is not str or snapshot.last_status not in _LAST_STATUSES:
        _invalid()
    category = snapshot.last_failure_category
    if type(category) is not str or category not in {*WORKER_ERROR_CATEGORIES, ""}:
        _invalid()
    if (snapshot.last_status == "failed") != bool(category):
        _invalid()
    if snapshot.in_flight != (snapshot.last_status == "running"):
        _invalid()
    if snapshot.last_completed_receipt is not None:
        _validate_receipt(snapshot.last_completed_receipt)


def empty_snapshot_v1() -> IndexRefreshObservabilitySnapshotV1:
    return IndexRefreshObservabilitySnapshotV1(
        available=True,
        in_flight=False,
        attempt_count=0,
        idle_count=0,
        completed_count=0,
        failed_count=0,
        cancelled_count=0,
        consecutive_failures=0,
        backoff_seconds=0,
        last_status="none",
        last_failure_category="",
        last_completed_receipt=None,
    )


class IndexRefreshObservabilityV1:
    """Non-durable counters. Invalidation never changes the worker's behavior."""

    __slots__ = (
        "_lock", "_available", "_in_flight", "_counts", "_consecutive_failures",
        "_backoff_seconds", "_last_status", "_last_failure_category", "_receipt",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._available = True
        self._in_flight = False
        self._counts = dict.fromkeys(("attempt", "idle", "completed", "failed", "cancelled"), 0)
        self._consecutive_failures = 0
        self._backoff_seconds = 0
        self._last_status = "none"
        self._last_failure_category = ""
        self._receipt = None

    def __repr__(self) -> str:
        return "<IndexRefreshObservabilityV1>"

    def invalidate(self) -> None:
        with self._lock:
            self._available = False

    def record_attempt(self) -> None:
        with self._lock:
            self._counts["attempt"] = _increment(self._counts["attempt"])
            self._in_flight = True
            self._backoff_seconds = 0
            self._last_status = "running"
            self._last_failure_category = ""

    def _finish(self, status: str) -> None:
        self._counts[status] = _increment(self._counts[status])
        self._in_flight = False
        self._last_status = status
        self._last_failure_category = ""
        self._backoff_seconds = 0

    def record_idle(self) -> None:
        with self._lock:
            self._finish("idle")
            self._consecutive_failures = 0

    def record_completed(
        self, *, batch_pending_count: object, batch_completed_count: object,
        rebuilt: object, source_atomic_count: object, bm25_document_count: object,
        vector_document_count: object, provider_call_count: object,
    ) -> None:
        pending = _count(batch_pending_count)
        completed = _count(batch_completed_count)
        if batch_completed_count > batch_pending_count:
            _invalid()
        receipt = IndexRefreshReceiptSummaryV1(
            batch_pending_count=pending,
            batch_completed_count=completed,
            rebuilt=rebuilt,
            source_atomic_count=_count(source_atomic_count),
            bm25_document_count=_count(bm25_document_count),
            vector_document_count=_count(vector_document_count),
            provider_call_count=_count(provider_call_count),
        )
        with self._lock:
            self._finish("completed")
            self._consecutive_failures = 0
            self._receipt = receipt

    def record_failed(self, category: object, backoff_seconds: object) -> None:
        safe = (
            category if type(category) is str and category in WORKER_ERROR_CATEGORIES
            else "memory_index_refresh_worker_error"
        )
        if (
            type(backoff_seconds) not in (int, float)
            or not 0 <= backoff_seconds <= MAX_BACKOFF_SECONDS
        ):
            _invalid()
        with self._lock:
            self._finish("failed")
            self._last_failure_category = safe
            self._consecutive_failures = _increment(self._consecutive_failures)
            self._backoff_seconds = int(backoff_seconds)

    def record_cancelled(self) -> None:
        with self._lock:
            # Cancelling polling/backoff is not another cancelled drain attempt.
            if self._in_flight:
                self._finish("cancelled")
            self._backoff_seconds = 0

    def snapshot(self) -> IndexRefreshObservabilitySnapshotV1:
        with self._lock:
            return IndexRefreshObservabilitySnapshotV1(
                available=self._available,
                in_flight=self._in_flight,
                attempt_count=self._counts["attempt"],
                idle_count=self._counts["idle"],
                completed_count=self._counts["completed"],
                failed_count=self._counts["failed"],
                cancelled_count=self._counts["cancelled"],
                consecutive_failures=self._consecutive_failures,
                backoff_seconds=self._backoff_seconds,
                last_status=self._last_status,
                last_failure_category=self._last_failure_category,
                last_completed_receipt=self._receipt,
            )


def project_status_payload_v1(
    snapshot: object, *, enabled: bool, installed: bool, shadow_enabled: bool,
    active_enabled: bool, mode: str, task_state: str, observability_available: bool,
) -> dict:
    _validate_snapshot(snapshot)
    if any(type(value) is not bool for value in (
        enabled, installed, shadow_enabled, active_enabled, observability_available,
    )):
        _invalid()
    if type(mode) is not str or mode not in _MODES:
        _invalid()
    if type(task_state) is not str or task_state not in _TASK_STATES:
        _invalid()
    if observability_available and (mode == "unavailable" or task_state == "unavailable"):
        _invalid()
    if mode != "unavailable":
        expected_flags = {
            "disabled": (False, False),
            "worker_only": (True, False),
            "worker_readonly_shadow": (True, True),
            "legacy_query_repair": (False, True),
        }[mode]
        if not installed or active_enabled or (enabled, shadow_enabled) != expected_flags:
            _invalid()
        if (task_state == "disabled") != (not enabled):
            _invalid()
    receipt = snapshot.last_completed_receipt
    return {
        "contract_version": OBSERVABILITY_CONTRACT_VERSION,
        "enabled": enabled,
        "installed": installed,
        "shadow_enabled": shadow_enabled,
        "active_enabled": active_enabled,
        "mode": mode,
        "observability_available": observability_available and snapshot.available,
        "task_state": task_state,
        "task_running": task_state == "running",
        "in_flight": snapshot.in_flight,
        "attempts": snapshot.attempt_count,
        "outcomes": {
            "idle": snapshot.idle_count,
            "completed": snapshot.completed_count,
            "failed": snapshot.failed_count,
            "cancelled": snapshot.cancelled_count,
        },
        "consecutive_failures": snapshot.consecutive_failures,
        "backoff_seconds": snapshot.backoff_seconds,
        "last": {
            "status": snapshot.last_status,
            "failure_category": snapshot.last_failure_category,
        },
        "last_completed_receipt": None if receipt is None else {
            **{name: getattr(receipt, name) for name in _RECEIPT_COUNTS},
            "rebuilt": receipt.rebuilt,
        },
    }


__all__ = (
    "IndexRefreshObservabilityV1", "IndexRefreshObservabilitySnapshotV1",
    "IndexRefreshReceiptSummaryV1", "MAX_COUNTER", "MAX_BACKOFF_SECONDS",
    "OBSERVABILITY_CONTRACT_VERSION", "WORKER_ERROR_CATEGORIES",
    "empty_snapshot_v1", "project_status_payload_v1",
)
