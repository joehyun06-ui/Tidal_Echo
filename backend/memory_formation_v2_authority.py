"""Fail-closed Atomic Memory Formation V2 write authority for Web/Kelivo.

The gate is independent and default-off.  When enabled, Web and Kelivo
canonical messages never execute the V1 formation task: V2 extraction is
performed through the reviewed localhost-only endpoint and accepted proposals
are persisted through the already-authorized candidate persistence runtime.
Telegram remains on the existing V1 path until it has an equivalent main-forward
completion barrier; authority is therefore partitioned by canonical channel and
there is never dual write authority for one message.

The same gate also swaps production candidate review and terminal decisions to
the V2-aware proof engine before application startup composes those capabilities.
No schema migration is introduced.
"""

from __future__ import annotations

import asyncio
import os
from typing import Final

from backend import (
    deployment_config,
    kelivo_service,
    memory_candidate_decision_adapters_v2,
    memory_candidate_decision_ledger,
    memory_candidate_decision_v2,
    memory_candidate_persistence_v2,
    memory_candidate_review,
    memory_candidate_review_v2,
    memory_formation_v2,
    memory_formation_v2_loopback,
    memory_formation_v2_runtime_patch,
)


ENV_GATE: Final = "MEMORY_FORMATION_V2_AUTHORITY_ENABLED"
INSTALL_MARKER: Final = "_MEMORY_FORMATION_V2_AUTHORITY_INSTALLED"
ENABLED_MARKER: Final = "_MEMORY_FORMATION_V2_AUTHORITY_ENABLED"

_ALLOWED_FAILURES: Final = frozenset({
    "extractor_invalid_output",
    "extractor_timeout",
    "extractor_unavailable",
    "invalid_source_text",
    "loopback_invalid_response",
    "loopback_unavailable",
    "source_ineligible",
    "source_unavailable",
    "auto_candidate_persistence_disabled",
    "candidate_budget_exceeded",
    "candidate_persistence_conflict",
    "candidate_persistence_failed",
    "candidate_policy_rejected",
    "candidate_state_conflict",
    "duplicate_proposal",
    "duplicate_span",
    "empty_spans",
    "formation_replay_conflict",
    "ineligible_proposal",
    "memory_configuration_invalid",
    "memory_fingerprint_profile_mismatch",
    "memory_schema_invalid",
    "overlapping_proposals",
    "overlapping_spans",
    "runtime_authority_invalid",
    "storage_unavailable",
    "too_many_proposals",
    "too_many_spans",
    "too_many_total_spans",
})


def _deployment_error(category: str):
    raise deployment_config.DeploymentConfigError(category)


def _bounded(value: object, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(parsed, maximum))


def _safe_failure(error: object, fallback: str = "candidate_persistence_failed") -> str:
    category = getattr(error, "category", "")
    return category if type(category) is str and category in _ALLOWED_FAILURES else fallback


def _log_failure(error: object) -> None:
    try:
        print(
            "[memory-formation-v2-authority] status=failed "
            f"category={_safe_failure(error)}",
            flush=True,
        )
    except BaseException:
        pass


def _log_success(proposals: tuple, result: object) -> None:
    try:
        total_spans = sum(len(proposal.spans) for proposal in proposals)
        multi_span = sum(1 for proposal in proposals if len(proposal.spans) > 1)
        print(
            "[memory-formation-v2-authority] status=completed "
            f"proposals={_bounded(getattr(result, 'proposal_count', 0), 3)} "
            f"candidates={_bounded(getattr(result, 'candidate_count', 0), 3)} "
            f"created={_bounded(getattr(result, 'created_count', 0), 3)} "
            f"existing={_bounded(getattr(result, 'existing_candidate_count', 0), 3)} "
            f"active_duplicates={_bounded(getattr(result, 'active_duplicate_count', 0), 3)} "
            f"suppressed={_bounded(getattr(result, 'suppressed_count', 0), 3)} "
            f"multi_span={_bounded(multi_span, 3)} "
            f"spans={_bounded(total_spans, 8)} "
            f"replayed={'true' if getattr(result, 'replayed', False) is True else 'false'}",
            flush=True,
        )
    except BaseException:
        pass


def _require_authority_config(relay_app: object) -> None:
    deployment = getattr(relay_app, "DEPLOYMENT", None)
    memory = getattr(deployment, "memory", None)
    if memory is None or not getattr(memory, "configuration_valid", False):
        _deployment_error("memory_formation_v2_authority_configuration_invalid")
    if not (
        memory.enabled
        and memory.auto_formation_enabled
        and memory.auto_candidate_persistence_enabled
        and memory.candidate_review_enabled
        and memory.candidate_decisions_enabled
    ):
        _deployment_error("memory_formation_v2_authority_requires_candidate_lifecycle")
    required = (
        "_run_memory_formation_shadow_task",
        "_run_natural_ingress_memory_formation_shadow_task",
        "_load_natural_ingress_formation_source",
        "_compose_memory_candidate_review",
        "_compose_memory_candidate_decisions",
        "MEMORY_CANDIDATE_PERSISTENCE",
        "MEMORY_PRIVILEGED_RUNTIME",
    )
    if any(not hasattr(relay_app, name) for name in required):
        _deployment_error("memory_formation_v2_authority_runtime_invalid")
    ingest_url = str(getattr(relay_app, "LOOP_INGEST_URL", "") or "")
    token = str(getattr(relay_app, "API_LOOP_INTERNAL_TOKEN", "") or "")
    try:
        memory_formation_v2_loopback._endpoint_from_ingest(ingest_url)
    except Exception:
        _deployment_error("memory_formation_v2_authority_loopback_invalid")
    if len(token) < 32:
        _deployment_error("memory_formation_v2_authority_loopback_invalid")


def _install_review_composition(relay_app: object) -> None:
    def compose_review_v2() -> None:
        relay_app.MEMORY_CANDIDATE_REVIEW_SERVICE = None
        relay_app.MEMORY_CANDIDATE_REVIEW_OPERATOR = None
        relay_app.MEMORY_CANDIDATE_REVIEW_MCP = None
        relay_app.MEMORY_CANDIDATE_REVIEW_ERROR = ""
        if relay_app.CORE_STARTUP_ERROR or relay_app.MEMORY_STARTUP_ERROR:
            relay_app.MEMORY_CANDIDATE_REVIEW_ERROR = "candidate_review_schema_invalid"
            return
        try:
            capabilities = memory_candidate_review_v2.compose_candidate_review_capabilities_v2(
                relay_app.DEPLOYMENT
            )
            relay_app.MEMORY_CANDIDATE_REVIEW_SERVICE = capabilities.service
            relay_app.MEMORY_CANDIDATE_REVIEW_OPERATOR = capabilities.operator_cli
            relay_app.MEMORY_CANDIDATE_REVIEW_MCP = capabilities.mcp
        except memory_candidate_review.MemoryCandidateReviewError as error:
            relay_app.MEMORY_CANDIDATE_REVIEW_ERROR = relay_app._safe_candidate_review_category(
                error
            )
        except Exception:
            relay_app.MEMORY_CANDIDATE_REVIEW_ERROR = "candidate_review_state_invalid"

    relay_app._compose_memory_candidate_review = compose_review_v2


def _install_decision_composition(relay_app: object) -> None:
    def compose_decisions_v2() -> None:
        relay_app.MEMORY_CANDIDATE_DECISION_SERVICE = None
        relay_app.MEMORY_CANDIDATE_DECISION_OPERATOR = None
        relay_app.MEMORY_CANDIDATE_DECISION_MCP = None
        relay_app.MEMORY_CANDIDATE_DECISION_ERROR = None
        if relay_app.CORE_STARTUP_ERROR or relay_app.MEMORY_STARTUP_ERROR:
            relay_app.MEMORY_CANDIDATE_DECISION_ERROR = "candidate_decision_schema_invalid"
            return
        if (
            relay_app.MEMORY_CANDIDATE_REVIEW_SERVICE is None
            or relay_app.MEMORY_CANDIDATE_REVIEW_ERROR
        ):
            relay_app.MEMORY_CANDIDATE_DECISION_ERROR = "candidate_decision_state_invalid"
            return
        runtime = relay_app.MEMORY_PRIVILEGED_RUNTIME
        base_writer = getattr(runtime, "candidate_decisions", None) if runtime is not None else None
        try:
            writer = memory_candidate_decision_v2.bind_candidate_decision_writer_v2(base_writer)
            ready, category = writer.readiness()
            if not ready:
                relay_app.MEMORY_CANDIDATE_DECISION_ERROR = relay_app._safe_candidate_decision_category(
                    category
                )
                return
            relay_app.MEMORY_CANDIDATE_DECISION_SERVICE = writer
            relay_app.MEMORY_CANDIDATE_DECISION_OPERATOR = (
                memory_candidate_decision_adapters_v2.bind_operator_cli(writer)
            )
            relay_app.MEMORY_CANDIDATE_DECISION_MCP = (
                memory_candidate_decision_adapters_v2.bind_mcp(writer)
            )
        except memory_candidate_decision_ledger.MemoryCandidateDecisionLedgerError as error:
            relay_app.MEMORY_CANDIDATE_DECISION_ERROR = relay_app._safe_candidate_decision_category(
                error
            )
        except Exception:
            relay_app.MEMORY_CANDIDATE_DECISION_ERROR = "candidate_decision_state_invalid"

    relay_app._compose_memory_candidate_decisions = compose_decisions_v2


def _persistence_v2(relay_app: object):
    base = getattr(relay_app, "MEMORY_CANDIDATE_PERSISTENCE", None)
    return memory_candidate_persistence_v2.bind_candidate_persistence_v2(base)


async def _persist_source(relay_app: object, canonical_message_id: int, source_text: str) -> None:
    try:
        extraction = await memory_formation_v2_loopback.extract_v2_via_loopback(
            ingest_url=relay_app.LOOP_INGEST_URL,
            internal_token=relay_app.API_LOOP_INTERNAL_TOKEN,
            source_text=source_text,
        )
        proposals = memory_formation_v2.validate_auto_memory_proposals(
            extraction.proposals,
            source_length=len(source_text),
        )
        # Prove the deterministic builder before any persistence boundary.
        memory_formation_v2.build_auto_memory_candidates_v2(
            canonical_message_id,
            source_text,
            proposals,
            max_item_chars=relay_app.DEPLOYMENT.memory.max_item_chars,
        )
        persistence = _persistence_v2(relay_app)
        result = await asyncio.to_thread(
            persistence.persist,
            canonical_message_id=canonical_message_id,
            source_text=source_text,
            proposals=proposals,
        )
        if getattr(result, "outcome", "") != "completed":
            raise RuntimeError("candidate_persistence_failed")
        _log_success(proposals, result)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _log_failure(error)


async def _run_kelivo_authority(relay_app: object, **kwargs) -> None:
    try:
        canonical = await asyncio.to_thread(
            kelivo_service.load_completed_canonical_formation_source,
            relay_app.DB_PATH,
            kwargs["client_id"],
            kwargs["idempotency_key"],
            channel="kelivo",
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _log_failure(type("SourceFailure", (), {"category": "source_unavailable"})())
        return
    await _persist_source(relay_app, canonical.canonical_message_id, canonical.text)


async def _run_web_authority(relay_app: object, **kwargs) -> None:
    canonical_message_id = kwargs["canonical_message_id"]
    try:
        await memory_formation_v2_runtime_patch._wait_for_web_main_forward(
            canonical_message_id
        )
        canonical_id, source_text = await asyncio.to_thread(
            relay_app._load_natural_ingress_formation_source,
            canonical_message_id,
            channel=kwargs["channel"],
            source=kwargs["source"],
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        category = (
            "source_ineligible"
            if type(error).__name__ == "_TelegramAttachmentOnlyFormationSource"
            else "source_unavailable"
        )
        _log_failure(type("SourceFailure", (), {"category": category})())
        return
    await _persist_source(relay_app, canonical_id, source_text)


def _install_tasks(relay_app: object) -> None:
    original_natural = relay_app._run_natural_ingress_memory_formation_shadow_task

    async def kelivo_authority(**kwargs):
        await _run_kelivo_authority(relay_app, **kwargs)

    async def natural_partitioned(**kwargs):
        # Telegram retains V1 until a separate main-forward completion barrier
        # exists.  Web canonical messages are V2-only in authority mode.
        if kwargs.get("channel") != "web":
            await original_natural(**kwargs)
            return
        await _run_web_authority(relay_app, **kwargs)

    relay_app._run_memory_formation_shadow_task = kelivo_authority
    relay_app._run_natural_ingress_memory_formation_shadow_task = natural_partitioned


def install(relay_app: object) -> bool:
    """Install V2 write/review/decision authority when the strict gate is on."""

    if getattr(relay_app, INSTALL_MARKER, False):
        return bool(getattr(relay_app, ENABLED_MARKER, False))
    enabled = deployment_config.parse_strict_bool(
        os.environ.get(ENV_GATE, "false"),
        "invalid_memory_formation_v2_authority_enabled",
    )
    setattr(relay_app, ENABLED_MARKER, enabled)
    setattr(relay_app, INSTALL_MARKER, True)
    if not enabled:
        return False
    _require_authority_config(relay_app)
    # Install the Web main-forward barrier without installing the V1→V2 shadow
    # wrappers.  p3_relay_app skips shadow.install() while authority is active.
    memory_formation_v2_runtime_patch._install_web_forward_tracking(relay_app)
    _install_review_composition(relay_app)
    _install_decision_composition(relay_app)
    _install_tasks(relay_app)
    return True
