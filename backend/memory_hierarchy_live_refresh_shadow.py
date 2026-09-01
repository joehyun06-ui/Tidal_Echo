"""Live semantic hierarchy refresh shadow for Phase 4D-B6F.

This independently gated runtime layer waits for the B6E startup pass, then runs
semantic B4/B5 hierarchy refinement followed by the existing B6 v2 derived-text
rebuild. After startup it listens only to already-committed active-Memory
terminal mutation boundaries. Pending candidate creation/review/reject paths do
not trigger it.

Triggers are coalesced through one event-loop worker. Before any semantic
provider call, the worker recomputes the authoritative active Atomic snapshot
digest; if it equals the last fully processed digest the pass is skipped. A pass
with refinement-provider fallback or summary failures is not marked complete, so
the same revision remains retryable on a later trigger. No hierarchy or summary
output is exposed to retrieval/context authority.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from functools import partial
from typing import Final

from backend import (
    deployment_config,
    memory_candidate_decision_ledger,
    memory_candidate_decision_v2,
    memory_explicit_actions,
    memory_hierarchy_baseline,
    memory_hierarchy_refinement_loopback,
    memory_hierarchy_semantic_rebuild,
    memory_hierarchy_summary_loopback_v2,
    memory_hierarchy_summary_rebuild_v2,
    memory_hierarchy_summary_runtime_shadow,
    memory_service,
)


ENV_GATE: Final = "MEMORY_HIERARCHY_LIVE_REFRESH_SHADOW_ENABLED"
INSTALL_MARKER: Final = "_MEMORY_HIERARCHY_LIVE_REFRESH_SHADOW_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_HIERARCHY_LIVE_REFRESH_SHADOW_ENABLED"
TASK_MARKER: Final = "_MEMORY_HIERARCHY_LIVE_REFRESH_SHADOW_TASK"

_TRIGGER_STARTUP: Final = "startup"
_TRIGGER_EXPLICIT: Final = "explicit_terminal"
_TRIGGER_APPROVE: Final = "candidate_approve"
_ALLOWED_TRIGGERS: Final = frozenset({
    _TRIGGER_STARTUP,
    _TRIGGER_EXPLICIT,
    _TRIGGER_APPROVE,
})

_ALLOWED_FAILURES: Final = frozenset({
    "hierarchy_summary_cache_invalid",
    "hierarchy_summary_projection_invalid",
    "hierarchy_summary_rebuild_failed",
    "hierarchy_summary_source_invalid",
    "loopback_unavailable",
    "semantic_rebuild_configuration_invalid",
    "semantic_rebuild_failed",
    "semantic_rebuild_projection_invalid",
    "semantic_rebuild_source_invalid",
    "memory_hierarchy_live_refresh_shadow_unavailable",
})


class MemoryHierarchyLiveRefreshShadowError(RuntimeError):
    __slots__ = ("category",)

    def __init__(self, category: object):
        safe = (
            category
            if type(category) is str and category in _ALLOWED_FAILURES
            else "memory_hierarchy_live_refresh_shadow_unavailable"
        )
        self.category = safe
        super().__init__(safe)


def enabled_from_environment(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return deployment_config.parse_strict_bool(
        env.get(ENV_GATE, "false"),
        "invalid_memory_hierarchy_live_refresh_shadow_enabled",
    )


def _safe_failure(error: object) -> str:
    category = getattr(error, "category", "")
    if category in _ALLOWED_FAILURES:
        return category
    return "memory_hierarchy_live_refresh_shadow_unavailable"


def _prompt_contract_version(relay_app: object) -> str:
    service = getattr(relay_app, "kelivo_service", None)
    value = getattr(service, "PROMPT_CONTRACT_VERSION", "kelivo-provider-prompt-v1")
    if type(value) is not str or not value:
        return "kelivo-provider-prompt-v1"
    return value


def _snapshot_digest(relay_app: object) -> str:
    reader = memory_hierarchy_summary_runtime_shadow._reader(relay_app)
    snapshot = reader.load_active_snapshot()
    plan = memory_hierarchy_baseline.build_baseline_hierarchy_plan_v1(
        snapshot.atomics,
    )
    return plan.atomic_snapshot_digest


def _bounded(value: object, maximum: int = 10000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(parsed, maximum))


async def _run_refresh_pass(relay_app: object):
    reader = memory_hierarchy_summary_runtime_shadow._reader(relay_app)
    hierarchy_path, summary_path = memory_hierarchy_summary_runtime_shadow._paths(relay_app)
    defaults = deployment_config.resolve_kelivo_provider_contract_defaults(
        os.environ,
        relay_app.DEPLOYMENT.loop_config,
    )
    prompt_contract = _prompt_contract_version(relay_app)
    refinement_generation = partial(
        memory_hierarchy_refinement_loopback.generate_via_loopback,
        ingest_url=relay_app.LOOP_INGEST_URL,
        internal_token=relay_app.API_LOOP_INTERNAL_TOKEN,
    )
    semantic = await memory_hierarchy_semantic_rebuild.rebuild_semantic_hierarchy_v1(
        reader,
        hierarchy_path,
        refinement_generation,
        provider_model=defaults.provider_model,
        provider_prompt_contract_version=prompt_contract,
    )
    summary_generation = partial(
        memory_hierarchy_summary_loopback_v2.generate_v2_via_loopback,
        ingest_url=relay_app.LOOP_INGEST_URL,
        internal_token=relay_app.API_LOOP_INTERNAL_TOKEN,
    )
    summaries = await memory_hierarchy_summary_rebuild_v2.rebuild_current_hierarchy_summaries_v2(
        reader,
        hierarchy_path,
        summary_path,
        summary_generation,
        provider_model=defaults.provider_model,
        provider_prompt_contract_version=prompt_contract,
    )
    return semantic, summaries


def _log_completed(semantic, summaries, *, trigger_count: int) -> None:
    status = (
        "completed"
        if not semantic.provider_failed and summaries.failed_count == 0
        else "completed_with_failures"
    )
    print(
        "[memory-hierarchy-live-refresh-shadow] "
        f"status={status} "
        f"triggers={_bounded(trigger_count)} "
        f"atomics={_bounded(semantic.atomic_count)} "
        f"topics={_bounded(semantic.topic_count)} "
        f"episodes={_bounded(semantic.episode_count)} "
        f"nodes={_bounded(semantic.node_count)} "
        f"dirty={_bounded(semantic.dirty_node_count)} "
        f"topic_mode={semantic.topic_mode} "
        f"topic_provider_calls={_bounded(semantic.topic_provider_call_count)} "
        f"episode_mode={semantic.episode_mode} "
        f"episode_provider_calls={_bounded(semantic.episode_provider_call_count)} "
        f"summary_targets={_bounded(summaries.target_count)} "
        f"summary_hits={_bounded(summaries.cache_hit_count)} "
        f"summary_generated={_bounded(summaries.generated_count)} "
        f"summary_failed={_bounded(summaries.failed_count)} "
        f"summary_pruned={_bounded(summaries.pruned_count)} "
        f"summary_provider_calls={_bounded(summaries.provider_call_count)}",
        file=sys.stderr,
        flush=True,
    )


def _log_unchanged(*, trigger_count: int) -> None:
    print(
        "[memory-hierarchy-live-refresh-shadow] "
        f"status=unchanged triggers={_bounded(trigger_count)} provider_calls=0",
        file=sys.stderr,
        flush=True,
    )


def _log_failed(error: object) -> None:
    print(
        "[memory-hierarchy-live-refresh-shadow] "
        f"status=failed category={_safe_failure(error)}",
        file=sys.stderr,
        flush=True,
    )


async def _await_b6e_startup(relay_app: object) -> None:
    task = getattr(
        relay_app,
        memory_hierarchy_summary_runtime_shadow.TASK_MARKER,
        None,
    )
    if isinstance(task, asyncio.Task) and task is not asyncio.current_task():
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def _worker(
    relay_app: object,
    event: asyncio.Event,
    pending: set[str],
) -> None:
    last_fully_processed_digest: str | None = None
    await _await_b6e_startup(relay_app)
    while True:
        await event.wait()
        event.clear()
        trigger_count = len(pending)
        pending.clear()
        try:
            current_digest = await asyncio.to_thread(_snapshot_digest, relay_app)
            if (
                last_fully_processed_digest is not None
                and current_digest == last_fully_processed_digest
            ):
                _log_unchanged(trigger_count=trigger_count)
                continue
            semantic, summaries = await _run_refresh_pass(relay_app)
            if not semantic.provider_failed and summaries.failed_count == 0:
                last_fully_processed_digest = semantic.atomic_snapshot_digest
            _log_completed(semantic, summaries, trigger_count=trigger_count)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_failed(error)


def _install_mutation_hooks(
    loop: asyncio.AbstractEventLoop,
    event: asyncio.Event,
    pending: set[str],
):
    originals = (
        memory_explicit_actions.MemoryActionEntryBackend._run,
        memory_candidate_decision_v2.CandidateDecisionWriterV2.decide,
        memory_service.CandidateDecisionWriter.decide,
    )

    def enqueue(trigger: str) -> None:
        if trigger not in _ALLOWED_TRIGGERS:
            return

        def mark() -> None:
            pending.add(trigger)
            event.set()

        try:
            loop.call_soon_threadsafe(mark)
        except RuntimeError:
            pass

    original_explicit, original_v2_decide, original_v1_decide = originals

    def explicit_run(self, *args, **kwargs):
        result = original_explicit(self, *args, **kwargs)
        if (
            type(result) is memory_explicit_actions.ExplicitMemoryActionResult
            and result.status == "completed"
            and result.replayed is False
            and result.category != "suppressed"
        ):
            enqueue(_TRIGGER_EXPLICIT)
        return result

    def v2_decide(self, *, binding):
        result = original_v2_decide(self, binding=binding)
        if (
            type(result) is memory_candidate_decision_ledger.CandidateDecisionResultV1
            and result.status == "completed"
            and result.decision == "approve"
            and result.replayed is False
        ):
            enqueue(_TRIGGER_APPROVE)
        return result

    def v1_decide(self, *, binding):
        result = original_v1_decide(self, binding=binding)
        if (
            type(result) is memory_candidate_decision_ledger.CandidateDecisionResultV1
            and result.status == "completed"
            and result.decision == "approve"
            and result.replayed is False
        ):
            enqueue(_TRIGGER_APPROVE)
        return result

    memory_explicit_actions.MemoryActionEntryBackend._run = explicit_run
    memory_candidate_decision_v2.CandidateDecisionWriterV2.decide = v2_decide
    memory_service.CandidateDecisionWriter.decide = v1_decide
    return originals


def _restore_mutation_hooks(originals) -> None:
    if type(originals) is not tuple or len(originals) != 3:
        return
    (
        memory_explicit_actions.MemoryActionEntryBackend._run,
        memory_candidate_decision_v2.CandidateDecisionWriterV2.decide,
        memory_service.CandidateDecisionWriter.decide,
    ) = originals


def install(relay_app: object) -> bool:
    """Install the live semantic shadow worker behind an independent strict gate."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))
    enabled = enabled_from_environment(os.environ)
    setattr(relay_app, INSTALL_MARKER, True)
    setattr(relay_app, ENABLED_MARKER, enabled)
    if not enabled:
        return False

    if not bool(
        getattr(
            relay_app,
            memory_hierarchy_summary_runtime_shadow.ENABLED_MARKER,
            False,
        )
    ):
        raise deployment_config.DeploymentConfigError(
            "memory_hierarchy_live_refresh_shadow_requires_summary_shadow"
        )
    memory = relay_app.DEPLOYMENT.memory
    if not memory.enabled or not memory.configuration_valid:
        raise deployment_config.DeploymentConfigError(
            "memory_hierarchy_live_refresh_shadow_requires_memory"
        )

    app = relay_app.app
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def live_refresh_lifespan(application):
        async with original_lifespan(application):
            loop = asyncio.get_running_loop()
            event = asyncio.Event()
            pending: set[str] = {_TRIGGER_STARTUP}
            event.set()
            originals = _install_mutation_hooks(loop, event, pending)
            task = asyncio.create_task(_worker(relay_app, event, pending))
            setattr(relay_app, TASK_MARKER, task)
            try:
                yield
            finally:
                _restore_mutation_hooks(originals)
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                setattr(relay_app, TASK_MARKER, None)

    app.router.lifespan_context = live_refresh_lifespan
    return True
