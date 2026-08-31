"""P3 runtime wiring for comparison-only Atomic Memory Formation V2 shadow.

This module is intentionally a patch layer over the reviewed V1 formation flow.
When its independent environment gate is enabled, it wraps the existing V1
shadow task so V1 finishes first (including any V1 candidate persistence) and
only then runs the write-impossible V2 shadow against the same canonical source.

V2 never receives a persistence capability, Memory store, runtime authority, or
accepted-proposals callback. The only externally visible effect is bounded,
data-free structural telemetry.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from typing import Final

from backend import (
    deployment_config,
    kelivo_service,
    memory_formation_extractor_v2,
    memory_formation_integration_v2,
)


ENV_GATE: Final = "MEMORY_FORMATION_V2_SHADOW_ENABLED"
INSTALL_MARKER: Final = "_MEMORY_FORMATION_V2_SHADOW_RUNTIME_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_FORMATION_V2_SHADOW_RUNTIME_ENABLED"
_CAPTURE_MARKER: Final = "_MEMORY_FORMATION_V2_SHADOW_CAPTURE_INSTALLED"

_ALLOWED_V2_CATEGORIES: Final = frozenset({
    "candidate_rejected",
    "extractor_invalid_output",
    "extractor_timeout",
    "extractor_unavailable",
    "source_ineligible",
    "source_unavailable",
})

_LAST_V1_RESULT: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "memory_formation_v2_shadow_last_v1_result",
    default=None,
)


def _bounded(value: object, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(parsed, maximum))


def _v1_projection(result: object | None) -> tuple[str, int, int]:
    status = getattr(result, "status", "")
    if status not in {"completed", "failed"}:
        return "unavailable", 0, 0
    return (
        status,
        _bounded(getattr(result, "proposal_count", 0), 3),
        _bounded(getattr(result, "candidate_count", 0), 3),
    )


def _log_receipt(relay_app: object, v2_result: object, v1_result: object | None) -> None:
    """Emit one bounded V1/V2 comparison receipt without source or identifiers."""

    try:
        v1_status, v1_proposals, v1_candidates = _v1_projection(v1_result)
        v2_status = getattr(v2_result, "status", "")
        if v2_status == "completed":
            print(
                "[memory-formation-v2-shadow] status=completed "
                f"v1_status={v1_status} "
                f"v1_proposals={v1_proposals} "
                f"v1_candidates={v1_candidates} "
                f"v2_proposals={_bounded(getattr(v2_result, 'proposal_count', 0), 3)} "
                f"v2_candidates={_bounded(getattr(v2_result, 'candidate_count', 0), 3)} "
                f"v2_multi_span={_bounded(getattr(v2_result, 'multi_span_candidate_count', 0), 3)} "
                f"v2_spans={_bounded(getattr(v2_result, 'total_span_count', 0), 8)}",
                flush=True,
            )
            return
        category = getattr(v2_result, "category", "")
        safe_category = (
            category
            if type(category) is str and category in _ALLOWED_V2_CATEGORIES
            else "extractor_unavailable"
        )
        print(
            "[memory-formation-v2-shadow] status=failed "
            f"category={safe_category} "
            f"v1_status={v1_status} "
            f"v1_proposals={v1_proposals} "
            f"v1_candidates={v1_candidates}",
            flush=True,
        )
    except BaseException:
        # Comparison telemetry must never affect request, persistence, or shutdown.
        pass


def _log_fixed_failure(
    category: str,
    v1_result: object | None,
) -> None:
    class _Failure:
        status = "failed"

        def __init__(self, fixed: str):
            self.category = fixed

    _log_receipt(None, _Failure(category), v1_result)


async def _run_v2_for_kelivo(
    relay_app: object,
    *,
    client_id: str,
    idempotency_key: str,
    provider_model: str,
    generation_callable,
    v1_result: object | None,
) -> None:
    try:
        canonical = await asyncio.to_thread(
            kelivo_service.load_completed_canonical_formation_source,
            relay_app.DB_PATH,
            client_id,
            idempotency_key,
            channel="kelivo",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log_fixed_failure("source_unavailable", v1_result)
        return

    async def extractor(source_text: str):
        return await memory_formation_extractor_v2.extract_auto_memory_proposals_v2(
            generation_callable,
            source_text,
            provider_model=provider_model,
            provider_prompt_contract_version=kelivo_service.PROMPT_CONTRACT_VERSION,
        )

    try:
        result = await memory_formation_integration_v2.run_memory_formation_v2_shadow(
            canonical.canonical_message_id,
            canonical.text,
            extractor,
            max_item_chars=relay_app.DEPLOYMENT.memory.max_item_chars,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log_fixed_failure("extractor_unavailable", v1_result)
        return
    _log_receipt(relay_app, result, v1_result)


async def _run_v2_for_natural_ingress(
    relay_app: object,
    *,
    canonical_message_id: int,
    channel: str,
    source: str,
    generation_callable,
    v1_result: object | None,
) -> None:
    try:
        canonical_id, source_text = await asyncio.to_thread(
            relay_app._load_natural_ingress_formation_source,
            canonical_message_id,
            channel=channel,
            source=source,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if type(error).__name__ == "_TelegramAttachmentOnlyFormationSource":
            _log_fixed_failure("source_ineligible", v1_result)
        else:
            _log_fixed_failure("source_unavailable", v1_result)
        return

    try:
        provider_defaults = await asyncio.to_thread(
            deployment_config.resolve_kelivo_provider_contract_defaults,
            os.environ,
            relay_app.DEPLOYMENT.loop_config,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log_fixed_failure("extractor_unavailable", v1_result)
        return

    async def extractor(text: str):
        return await memory_formation_extractor_v2.extract_auto_memory_proposals_v2(
            generation_callable,
            text,
            provider_model=provider_defaults.provider_model,
            provider_prompt_contract_version=kelivo_service.PROMPT_CONTRACT_VERSION,
        )

    try:
        result = await memory_formation_integration_v2.run_memory_formation_v2_shadow(
            canonical_id,
            source_text,
            extractor,
            max_item_chars=relay_app.DEPLOYMENT.memory.max_item_chars,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log_fixed_failure("extractor_unavailable", v1_result)
        return
    _log_receipt(relay_app, result, v1_result)


def _install_v1_result_capture(relay_app: object) -> None:
    integration = relay_app.memory_formation_integration
    if getattr(integration, _CAPTURE_MARKER, False):
        return
    original = integration.run_memory_formation_shadow

    async def captured(*args, **kwargs):
        result = await original(*args, **kwargs)
        _LAST_V1_RESULT.set(result)
        return result

    integration.run_memory_formation_shadow = captured
    setattr(integration, _CAPTURE_MARKER, True)


def _install_task_wrappers(relay_app: object) -> None:
    original_kelivo = relay_app._run_memory_formation_shadow_task
    original_natural = relay_app._run_natural_ingress_memory_formation_shadow_task

    async def kelivo_wrapper(**kwargs):
        token = _LAST_V1_RESULT.set(None)
        try:
            await original_kelivo(**kwargs)
            v1_result = _LAST_V1_RESULT.get()
            await _run_v2_for_kelivo(
                relay_app,
                client_id=kwargs["client_id"],
                idempotency_key=kwargs["idempotency_key"],
                provider_model=kwargs["provider_model"],
                generation_callable=kwargs["generation_callable"],
                v1_result=v1_result,
            )
        finally:
            _LAST_V1_RESULT.reset(token)

    async def natural_wrapper(**kwargs):
        token = _LAST_V1_RESULT.set(None)
        try:
            await original_natural(**kwargs)
            v1_result = _LAST_V1_RESULT.get()
            await _run_v2_for_natural_ingress(
                relay_app,
                canonical_message_id=kwargs["canonical_message_id"],
                channel=kwargs["channel"],
                source=kwargs["source"],
                generation_callable=kwargs["generation_callable"],
                v1_result=v1_result,
            )
        finally:
            _LAST_V1_RESULT.reset(token)

    relay_app._run_memory_formation_shadow_task = kelivo_wrapper
    relay_app._run_natural_ingress_memory_formation_shadow_task = natural_wrapper


def install(relay_app: object) -> bool:
    """Install the independently gated V2 comparison after the reviewed V1 flow."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))
    enabled = deployment_config.parse_strict_bool(
        os.environ.get(ENV_GATE, "false"),
        "invalid_memory_formation_v2_shadow_enabled",
    )
    setattr(relay_app, ENABLED_MARKER, enabled)
    setattr(relay_app, INSTALL_MARKER, True)
    if not enabled:
        return False
    deployment = getattr(relay_app, "DEPLOYMENT", None)
    memory = getattr(deployment, "memory", None)
    if memory is None or memory.auto_formation_enabled is not True:
        raise deployment_config.DeploymentConfigError(
            "memory_formation_v2_shadow_requires_auto_formation"
        )
    required = (
        "_run_memory_formation_shadow_task",
        "_run_natural_ingress_memory_formation_shadow_task",
        "_load_natural_ingress_formation_source",
        "memory_formation_integration",
    )
    if any(not hasattr(relay_app, name) for name in required):
        raise deployment_config.DeploymentConfigError(
            "memory_formation_v2_shadow_runtime_invalid"
        )
    _install_v1_result_capture(relay_app)
    _install_task_wrappers(relay_app)
    return True
