"""P3 runtime wiring for comparison-only Atomic Memory Formation V2 shadow.

When the independent V2 shadow gate is enabled this patch preserves V1 as the
sole durable formation authority, waits for a Web/API main forward to finish
before natural-ingress formation begins, runs V1 to completion, and only then
runs V2 through its strict localhost-only extractor endpoint.

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
    memory_formation_v2_loopback,
)


ENV_GATE: Final = "MEMORY_FORMATION_V2_SHADOW_ENABLED"
INSTALL_MARKER: Final = "_MEMORY_FORMATION_V2_SHADOW_RUNTIME_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_FORMATION_V2_SHADOW_RUNTIME_ENABLED"
_CAPTURE_MARKER: Final = "_MEMORY_FORMATION_V2_SHADOW_CAPTURE_INSTALLED"
_FORWARD_MARKER: Final = "_MEMORY_FORMATION_V2_WEB_FORWARD_TRACKING_INSTALLED"

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
_WEB_FORWARD_BARRIERS: dict[int, asyncio.Future] = {}


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
        pass


def _log_fixed_failure(category: str, v1_result: object | None) -> None:
    class _Failure:
        status = "failed"

        def __init__(self, fixed: str):
            self.category = fixed

    _log_receipt(None, _Failure(category), v1_result)


def _v2_loopback_extractor(relay_app: object):
    async def extractor(source_text: str):
        try:
            return await memory_formation_v2_loopback.extract_v2_via_loopback(
                ingest_url=relay_app.LOOP_INGEST_URL,
                internal_token=relay_app.API_LOOP_INTERNAL_TOKEN,
                source_text=source_text,
            )
        except asyncio.CancelledError:
            raise
        except memory_formation_v2_loopback.MemoryFormationV2LoopbackError as error:
            category = (
                error.category
                if error.category in {
                    "extractor_invalid_output",
                    "extractor_timeout",
                    "extractor_unavailable",
                }
                else "extractor_unavailable"
            )
            raise memory_formation_extractor_v2.MemoryFormationExtractorV2Error(
                category
            ) from None

    return extractor


async def _run_v2_for_kelivo(
    relay_app: object,
    *,
    client_id: str,
    idempotency_key: str,
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

    try:
        result = await memory_formation_integration_v2.run_memory_formation_v2_shadow(
            canonical.canonical_message_id,
            canonical.text,
            _v2_loopback_extractor(relay_app),
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
        result = await memory_formation_integration_v2.run_memory_formation_v2_shadow(
            canonical_id,
            source_text,
            _v2_loopback_extractor(relay_app),
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


def _install_web_forward_tracking(relay_app: object) -> None:
    """Create a barrier synchronously when Web schedules its main loop forward."""

    if getattr(relay_app, _FORWARD_MARKER, False):
        return
    original_forward = relay_app.forward_to_loop

    def tracked_forward(msg: object):
        message_id = msg.get("id") if isinstance(msg, dict) else None
        barrier = None
        if type(message_id) is int and message_id > 0:
            loop = asyncio.get_running_loop()
            barrier = loop.create_future()
            _WEB_FORWARD_BARRIERS[message_id] = barrier

        async def run():
            try:
                return await original_forward(msg)
            finally:
                if barrier is not None and not barrier.done():
                    barrier.set_result(None)
                if (
                    type(message_id) is int
                    and _WEB_FORWARD_BARRIERS.get(message_id) is barrier
                ):
                    _WEB_FORWARD_BARRIERS.pop(message_id, None)

        return run()

    relay_app.forward_to_loop = tracked_forward
    setattr(relay_app, _FORWARD_MARKER, True)


async def _wait_for_web_main_forward(canonical_message_id: object) -> None:
    if type(canonical_message_id) is not int or canonical_message_id <= 0:
        return
    barrier = _WEB_FORWARD_BARRIERS.get(canonical_message_id)
    if barrier is None:
        return
    await asyncio.shield(barrier)


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
                v1_result=v1_result,
            )
        finally:
            _LAST_V1_RESULT.reset(token)

    async def natural_wrapper(**kwargs):
        token = _LAST_V1_RESULT.set(None)
        try:
            if (kwargs.get("channel"), kwargs.get("source")) == ("web", "relay"):
                await _wait_for_web_main_forward(kwargs.get("canonical_message_id"))
            await original_natural(**kwargs)
            v1_result = _LAST_V1_RESULT.get()
            await _run_v2_for_natural_ingress(
                relay_app,
                canonical_message_id=kwargs["canonical_message_id"],
                channel=kwargs["channel"],
                source=kwargs["source"],
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
        "forward_to_loop",
        "LOOP_INGEST_URL",
        "API_LOOP_INTERNAL_TOKEN",
    )
    if any(not hasattr(relay_app, name) for name in required):
        raise deployment_config.DeploymentConfigError(
            "memory_formation_v2_shadow_runtime_invalid"
        )
    _install_v1_result_capture(relay_app)
    _install_web_forward_tracking(relay_app)
    _install_task_wrappers(relay_app)
    return True
