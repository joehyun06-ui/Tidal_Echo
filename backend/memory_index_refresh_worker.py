"""Default-off durable refresh worker for disposable Memory indexes.

The worker drains only content-free v11 dirty signals.  It fixes a pending
high watermark before reading the authoritative Atomic snapshot, proves or
rebuilds BM25 and vector sidecars from that later snapshot, and only then marks
the observed prefix complete.  Later events remain pending for another pass.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, Mapping

from backend import (
    deployment_config,
    memory_index_outbox_consumer as outbox,
    memory_retrieval_hybrid_runtime_active as runtime_active,
    memory_retrieval_hybrid_runtime_composition as composition,
    memory_retrieval_hybrid_runtime_shadow as runtime_shadow,
)


WORKER_CONTRACT_VERSION: Final = "memory-index-refresh-worker-v1"
ENV_GATE: Final = "MEMORY_INDEX_REFRESH_WORKER_ENABLED"
INSTALL_MARKER: Final = "_MEMORY_INDEX_REFRESH_WORKER_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_INDEX_REFRESH_WORKER_ENABLED"
TASK_MARKER: Final = "_MEMORY_INDEX_REFRESH_WORKER_TASK"

POLL_SECONDS: Final = 2.0
RECONCILE_SECONDS: Final = 60.0
MAX_BACKOFF_SECONDS: Final = 60.0

_ERROR_CATEGORIES: Final = frozenset({
    "memory_index_refresh_completion_failed",
    "memory_index_refresh_configuration_invalid",
    "memory_index_refresh_conflicts_runtime",
    "memory_index_refresh_outbox_read_failed",
    "memory_index_refresh_reconcile_failed",
    "memory_index_refresh_requires_memory",
    "memory_index_refresh_runner_invalid",
    "memory_index_refresh_worker_error",
})


class MemoryIndexRefreshWorkerError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ERROR_CATEGORIES
            else "memory_index_refresh_worker_error"
        )
        self.category = safe
        super().__init__(safe)

    def __str__(self) -> str:
        try:
            return object.__getattribute__(self, "category")
        except BaseException:
            return "memory_index_refresh_worker_error"

    def __repr__(self) -> str:
        return f"MemoryIndexRefreshWorkerError({str(self)!r})"


def _raise(category: str) -> None:
    raise MemoryIndexRefreshWorkerError(category)


def enabled_from_environment(
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    return deployment_config.parse_strict_bool(
        env.get(ENV_GATE, "false"),
        "invalid_memory_index_refresh_worker_enabled",
    )


@dataclass(frozen=True, slots=True, repr=False)
class MemoryIndexDrainReceiptV1:
    contract_version: str
    pending_count: int
    completed_count: int
    reconciled: bool
    rebuilt: bool
    source_atomic_count: int
    bm25_document_count: int
    vector_document_count: int
    provider_call_count: int

    def __repr__(self) -> str:
        return (
            "<MemoryIndexDrainReceiptV1 "
            f"pending={self.pending_count} completed={self.completed_count} "
            f"reconciled={self.reconciled!r} rebuilt={self.rebuilt!r} "
            f"provider_calls={self.provider_call_count}>"
        )


def _idle_receipt() -> MemoryIndexDrainReceiptV1:
    return MemoryIndexDrainReceiptV1(
        contract_version=WORKER_CONTRACT_VERSION,
        pending_count=0,
        completed_count=0,
        reconciled=False,
        rebuilt=False,
        source_atomic_count=0,
        bm25_document_count=0,
        vector_document_count=0,
        provider_call_count=0,
    )


def _outbox_read_error(error: outbox.MemoryIndexOutboxConsumerError) -> None:
    if error.category == "memory_index_outbox_configuration_invalid":
        _raise("memory_index_refresh_configuration_invalid")
    _raise("memory_index_refresh_outbox_read_failed")


async def drain_once_v1(
    database_path: object,
    runner: object,
    *,
    reconcile_without_pending: bool = False,
) -> MemoryIndexDrainReceiptV1:
    """Drain one fixed outbox prefix after one proved index reconciliation."""

    if type(reconcile_without_pending) is not bool:
        _raise("memory_index_refresh_configuration_invalid")
    try:
        batch = await asyncio.to_thread(
            outbox.peek_pending_batch_v1,
            database_path,
        )
    except asyncio.CancelledError:
        raise
    except outbox.MemoryIndexOutboxConsumerError as error:
        _outbox_read_error(error)
    except Exception:
        _raise("memory_index_refresh_outbox_read_failed")

    if batch is None and not reconcile_without_pending:
        return _idle_receipt()

    try:
        reconcile = getattr(runner, "reconcile_index_pair_v1")
        if not callable(reconcile):
            _raise("memory_index_refresh_runner_invalid")
        produced = reconcile()
        if not inspect.isawaitable(produced):
            _raise("memory_index_refresh_runner_invalid")
        index_receipt = await produced
        if type(index_receipt) is not composition.HybridIndexRefreshReceiptV1:
            _raise("memory_index_refresh_runner_invalid")
    except asyncio.CancelledError:
        raise
    except MemoryIndexRefreshWorkerError:
        raise
    except composition.MemoryRetrievalHybridRuntimeCompositionError:
        _raise("memory_index_refresh_reconcile_failed")
    except Exception:
        _raise("memory_index_refresh_reconcile_failed")

    completed_count = 0
    pending_count = 0 if batch is None else batch.pending_count
    if batch is not None:
        try:
            completion = await asyncio.to_thread(
                outbox.complete_pending_batch_v1,
                database_path,
                batch,
            )
            if type(completion) is not outbox.MemoryIndexCompletionReceiptV1:
                _raise("memory_index_refresh_completion_failed")
            completed_count = completion.completed_count
        except asyncio.CancelledError:
            raise
        except MemoryIndexRefreshWorkerError:
            raise
        except outbox.MemoryIndexOutboxConsumerError:
            _raise("memory_index_refresh_completion_failed")
        except Exception:
            _raise("memory_index_refresh_completion_failed")

    return MemoryIndexDrainReceiptV1(
        contract_version=WORKER_CONTRACT_VERSION,
        pending_count=pending_count,
        completed_count=completed_count,
        reconciled=True,
        rebuilt=index_receipt.rebuilt,
        source_atomic_count=index_receipt.source_atomic_count,
        bm25_document_count=index_receipt.bm25_document_count,
        vector_document_count=index_receipt.vector_document_count,
        provider_call_count=index_receipt.provider_call_count,
    )


def _log_line(line: str) -> None:
    try:
        print(line, file=sys.stderr, flush=True)
    except BaseException:
        pass


def _log_completed(receipt: MemoryIndexDrainReceiptV1) -> None:
    _log_line(
        "[memory-index-refresh] status=completed "
        f"pending={receipt.pending_count} completed={receipt.completed_count} "
        f"reconciled={str(receipt.reconciled).lower()} "
        f"rebuilt={str(receipt.rebuilt).lower()} "
        f"atomics={receipt.source_atomic_count} "
        f"bm25_documents={receipt.bm25_document_count} "
        f"vector_documents={receipt.vector_document_count} "
        f"provider_calls={receipt.provider_call_count}"
    )


def _log_failed(category: str) -> None:
    safe = category if category in _ERROR_CATEGORIES else "memory_index_refresh_worker_error"
    _log_line(f"[memory-index-refresh] status=failed category={safe}")


async def _worker(
    database_path: object,
    runner: object,
) -> None:
    next_reconcile = 0.0
    consecutive_failures = 0
    while True:
        try:
            now = time.monotonic()
            receipt = await drain_once_v1(
                database_path,
                runner,
                reconcile_without_pending=(now >= next_reconcile),
            )
            if receipt.reconciled:
                next_reconcile = time.monotonic() + RECONCILE_SECONDS
                _log_completed(receipt)
            consecutive_failures = 0
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            _log_line("[memory-index-refresh] status=cancelled")
            raise
        except MemoryIndexRefreshWorkerError as error:
            _log_failed(error.category)
            consecutive_failures = min(consecutive_failures + 1, 6)
            delay = min(
                POLL_SECONDS * (2 ** (consecutive_failures - 1)),
                MAX_BACKOFF_SECONDS,
            )
            await asyncio.sleep(delay)
        except Exception:
            _log_failed("memory_index_refresh_worker_error")
            consecutive_failures = min(consecutive_failures + 1, 6)
            delay = min(
                POLL_SECONDS * (2 ** (consecutive_failures - 1)),
                MAX_BACKOFF_SECONDS,
            )
            await asyncio.sleep(delay)


def _validate_runtime_requirements(
    relay_app: object,
    environ: Mapping[str, str],
) -> None:
    try:
        # A not-yet-installed shadow will be required to use C6's read-only
        # runner by its installer. An already-installed legacy writer cannot
        # be promoted in place. Active remains incompatible in either order.
        runtime_shadow.enabled_from_environment(environ)
        if (
            runtime_active.enabled_from_environment(environ)
            or bool(getattr(relay_app, runtime_active.ENABLED_MARKER, False))
            or (
                bool(getattr(relay_app, runtime_shadow.ENABLED_MARKER, False))
                and not bool(getattr(relay_app, runtime_shadow.READONLY_MARKER, False))
            )
        ):
            _raise("memory_index_refresh_conflicts_runtime")
        memory = relay_app.DEPLOYMENT.memory
        if not memory.enabled or not memory.configuration_valid:
            _raise("memory_index_refresh_requires_memory")
    except MemoryIndexRefreshWorkerError:
        raise
    except Exception:
        _raise("memory_index_refresh_configuration_invalid")


def install(
    relay_app: object,
    *,
    environ: Mapping[str, str] | None = None,
    runner: object = None,
) -> bool:
    """Install one lifespan worker only when the dedicated strict gate is ON."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))

    env = os.environ if environ is None else environ
    try:
        enabled = enabled_from_environment(env)
    except deployment_config.DeploymentConfigError:
        _raise("memory_index_refresh_configuration_invalid")
    if not enabled:
        setattr(relay_app, INSTALL_MARKER, True)
        setattr(relay_app, ENABLED_MARKER, False)
        return False

    _validate_runtime_requirements(relay_app, env)
    try:
        if runner is None:
            runner = composition.compose_hybrid_index_refresh_runner_v1(
                relay_app,
                env,
            )
        if (
            type(runner) is not composition.HybridRetrievalShadowRunnerV1
            or not callable(getattr(runner, "reconcile_index_pair_v1", None))
        ):
            _raise("memory_index_refresh_runner_invalid")
        database_path = runner.config.authority_path
        app = relay_app.app
        original_lifespan = app.router.lifespan_context
    except MemoryIndexRefreshWorkerError:
        raise
    except composition.MemoryRetrievalHybridRuntimeCompositionError:
        _raise("memory_index_refresh_configuration_invalid")
    except Exception:
        _raise("memory_index_refresh_configuration_invalid")

    @asynccontextmanager
    async def index_refresh_lifespan(application):
        async with original_lifespan(application):
            task = asyncio.create_task(
                _worker(database_path, runner),
                name="memory-index-refresh-worker",
            )
            setattr(relay_app, TASK_MARKER, task)
            try:
                yield
            finally:
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except BaseException:
                    pass
                setattr(relay_app, TASK_MARKER, None)

    app.router.lifespan_context = index_refresh_lifespan
    setattr(relay_app, ENABLED_MARKER, True)
    setattr(relay_app, INSTALL_MARKER, True)
    return True


__all__ = (
    "ENABLED_MARKER",
    "ENV_GATE",
    "INSTALL_MARKER",
    "MemoryIndexDrainReceiptV1",
    "MemoryIndexRefreshWorkerError",
    "TASK_MARKER",
    "WORKER_CONTRACT_VERSION",
    "drain_once_v1",
    "enabled_from_environment",
    "install",
)
