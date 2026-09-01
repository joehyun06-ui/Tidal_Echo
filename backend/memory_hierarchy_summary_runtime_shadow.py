"""Default-off production shadow wiring for Phase 4D-B6E.

When explicitly enabled, P3 schedules exactly one non-authoritative startup task:
complete active Atomic snapshot -> deterministic baseline hierarchy sidecar -> v2
missing/stale derived summary cache.  It never changes a response, Memory truth,
formation/review/decision authority, or retrieval input.

The task waits for the localhost api-loop to become ready, uses only the strict
summary-v2 loopback generation adapter, and emits bounded data-free counts.  A
failure is shadow-only and never fails application readiness after installation.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Final

from backend import (
    deployment_config,
    memory_hierarchy_rebuild,
    memory_hierarchy_snapshot,
    memory_hierarchy_summary_loopback_v2,
    memory_hierarchy_summary_rebuild_v2,
)


ENV_GATE: Final = "MEMORY_HIERARCHY_SUMMARY_SHADOW_ENABLED"
INSTALL_MARKER: Final = "_MEMORY_HIERARCHY_SUMMARY_SHADOW_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_HIERARCHY_SUMMARY_SHADOW_ENABLED"
TASK_MARKER: Final = "_MEMORY_HIERARCHY_SUMMARY_SHADOW_TASK"
HIERARCHY_FILENAME: Final = "memory-hierarchy-shadow.db"
SUMMARY_FILENAME: Final = "memory-hierarchy-summary-shadow-v2.db"
LOOP_READY_ATTEMPTS: Final = 10
LOOP_READY_DELAY_SECONDS: Final = 0.5

_ALLOWED_FAILURES: Final = frozenset({
    "hierarchy_rebuild_failed",
    "hierarchy_summary_cache_invalid",
    "hierarchy_summary_projection_invalid",
    "hierarchy_summary_rebuild_failed",
    "hierarchy_summary_source_invalid",
    "loopback_unavailable",
    "memory_hierarchy_summary_shadow_unavailable",
})


class MemoryHierarchySummaryRuntimeShadowError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ALLOWED_FAILURES
            else "memory_hierarchy_summary_shadow_unavailable"
        )
        self.category = safe
        super().__init__(safe)


def enabled_from_environment(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return deployment_config.parse_strict_bool(
        env.get(ENV_GATE, "false"),
        "invalid_memory_hierarchy_summary_shadow_enabled",
    )


def _paths(relay_app: object) -> tuple[Path, Path]:
    root = Path(relay_app.DEPLOYMENT.persistent_root)
    return root / HIERARCHY_FILENAME, root / SUMMARY_FILENAME


def _reader(relay_app: object) -> memory_hierarchy_snapshot.MemoryHierarchySnapshotReader:
    memory = relay_app.DEPLOYMENT.memory
    return memory_hierarchy_snapshot.MemoryHierarchySnapshotReader(
        relay_app.DEPLOYMENT.db_path,
        fingerprint_key_id=memory.fingerprint_key_id,
        fingerprint_hmac_secret=memory.fingerprint_hmac_secret,
        max_item_chars=memory.max_item_chars,
        sensitive_storage_enabled=memory.sensitive_storage_enabled,
    )


async def _wait_for_loop(relay_app: object) -> None:
    for attempt in range(LOOP_READY_ATTEMPTS):
        try:
            payload = await asyncio.to_thread(relay_app.loop_json, "/loop/config")
            if isinstance(payload, dict):
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if attempt + 1 < LOOP_READY_ATTEMPTS:
            await asyncio.sleep(LOOP_READY_DELAY_SECONDS)
    raise MemoryHierarchySummaryRuntimeShadowError("loopback_unavailable")


def _safe_failure(error: object) -> str:
    category = getattr(error, "category", "")
    if category in _ALLOWED_FAILURES:
        return category
    return "memory_hierarchy_summary_shadow_unavailable"


async def run_once(relay_app: object) -> None:
    """Run one startup projection/cache build and log structural counts only."""

    try:
        await _wait_for_loop(relay_app)
        reader = _reader(relay_app)
        hierarchy_path, summary_path = _paths(relay_app)
        hierarchy_receipt = await asyncio.to_thread(
            memory_hierarchy_rebuild.rebuild_baseline_hierarchy_v1,
            reader,
            hierarchy_path,
        )
        generation = partial(
            memory_hierarchy_summary_loopback_v2.generate_v2_via_loopback,
            ingest_url=relay_app.LOOP_INGEST_URL,
            internal_token=relay_app.API_LOOP_INTERNAL_TOKEN,
        )
        summary_receipt = await memory_hierarchy_summary_rebuild_v2.rebuild_current_hierarchy_summaries_v2(
            reader,
            hierarchy_path,
            summary_path,
            generation,
            provider_model=deployment_config.resolve_kelivo_provider_contract_defaults(
                os.environ,
                relay_app.DEPLOYMENT.loop_config,
            ).provider_model,
            provider_prompt_contract_version="kelivo-provider-prompt-v1",
        )
        print(
            "[memory-hierarchy-summary-shadow] "
            "status=completed "
            f"atomics={hierarchy_receipt.atomic_count} "
            f"topics={hierarchy_receipt.topic_count} "
            f"nodes={hierarchy_receipt.node_count} "
            f"dirty={hierarchy_receipt.dirty_node_count} "
            f"targets={summary_receipt.target_count} "
            f"hits={summary_receipt.cache_hit_count} "
            f"generated={summary_receipt.generated_count} "
            f"failed={summary_receipt.failed_count} "
            f"pruned={summary_receipt.pruned_count} "
            f"provider_calls={summary_receipt.provider_call_count}",
            file=sys.stderr,
            flush=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(
            "[memory-hierarchy-summary-shadow] "
            f"status=failed category={_safe_failure(error)}",
            file=sys.stderr,
            flush=True,
        )


def install(relay_app: object) -> bool:
    """Install one startup shadow task; gate OFF leaves app behavior unchanged."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))
    enabled = enabled_from_environment(os.environ)
    setattr(relay_app, INSTALL_MARKER, True)
    setattr(relay_app, ENABLED_MARKER, enabled)
    if not enabled:
        return False

    memory = relay_app.DEPLOYMENT.memory
    if not memory.enabled or not memory.configuration_valid:
        raise deployment_config.DeploymentConfigError(
            "memory_hierarchy_summary_shadow_requires_memory"
        )
    codex_entrypoints = deployment_config.parse_strict_bool(
        os.environ.get("CODEX_CANARY_ENTRYPOINTS_ENABLED", "false"),
        "invalid_codex_canary_entrypoints_enabled",
    )
    if not codex_entrypoints:
        raise deployment_config.DeploymentConfigError(
            "memory_hierarchy_summary_shadow_requires_codex_entrypoints"
        )

    app = relay_app.app
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def shadow_lifespan(application):
        async with original_lifespan(application):
            task = asyncio.create_task(run_once(relay_app))
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
                setattr(relay_app, TASK_MARKER, None)

    app.router.lifespan_context = shadow_lifespan
    return True
